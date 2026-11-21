"""Tests for Java language adapter, javac integration, and Java security analysis."""

import asyncio
from pathlib import Path

import pytest

from app.languages.java import JavaAdapter


@pytest.fixture
def java_adapter():
    return JavaAdapter()


def test_java_adapter_metadata(java_adapter):
    assert java_adapter.language_id == "java"
    assert java_adapter.display_name == "Java"
    assert java_adapter.capability_status == "production"
    assert java_adapter.source_extensions == {".java"}
    assert java_adapter.is_source_path("src/main/java/App.java") is True
    assert java_adapter.is_source_path("src/main/resources/config.xml") is False


def test_java_builtin_safety_analysis_detects_patterns(java_adapter, tmp_path):
    java_source = """package com.example.vulnerable;

import java.io.ObjectInputStream;
import java.security.MessageDigest;
import javax.crypto.spec.SecretKeySpec;
import java.sql.Statement;

public class VulnerableService {
    public void execute(ObjectInputStream in, Statement stmt, String userInput) {
        try {
            Object obj = in.readObject();
            stmt.executeQuery("SELECT * FROM users WHERE name = " + userInput);
            MessageDigest md = MessageDigest.getInstance("MD5");
            SecretKeySpec key = new SecretKeySpec("hardcoded1234567".getBytes(), "AES");
        } catch (Exception e) {}
    }
}
"""
    files = [("src/main/java/VulnerableService.java", java_source)]
    findings = java_adapter._run_builtin_analyzer(files, tmp_path)

    rule_ids = {f.title for f in findings}
    assert any("JAVA-SEC-DESERIALIZATION" in r for r in rule_ids)
    assert any("JAVA-SEC-SQLI" in r for r in rule_ids)
    assert any("JAVA-SEC-WEAK-HASH" in r for r in rule_ids)
    assert any("JAVA-SEC-HARDCODED-KEY" in r for r in rule_ids)
    assert any("JAVA-ERR-EMPTY-CATCH" in r for r in rule_ids)

    deser = next(f for f in findings if "JAVA-SEC-DESERIALIZATION" in f.title)
    assert deser.severity == "critical"
    assert deser.category == "security"

    sqli = next(f for f in findings if "JAVA-SEC-SQLI" in f.title)
    assert sqli.severity == "high"
    assert sqli.category == "security"


def test_java_safe_code_produces_no_findings(java_adapter, tmp_path):
    safe_code = """package com.example.safe;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.security.MessageDigest;

public class SafeService {
    public void findUser(Connection conn, String userId) throws Exception {
        String sql = "SELECT * FROM users WHERE id = ?";
        try (PreparedStatement stmt = conn.prepareStatement(sql)) {
            stmt.setString(1, userId);
            stmt.executeQuery();
        }
        MessageDigest md = MessageDigest.getInstance("SHA-256");
    }
}
"""
    files = [("src/main/java/SafeService.java", safe_code)]
    findings = java_adapter._run_builtin_analyzer(files, tmp_path)
    assert len(findings) == 0


def test_java_source_summarization(java_adapter):
    source = """package com.example.model;

import java.util.List;
import java.util.Optional;

public class OrderService {
    public Optional<String> getOrder(String id) {
        return Optional.empty();
    }
}

class InternalHelper {}
"""
    test_source = """package com.example.model;

import org.junit.jupiter.api.Test;

public class OrderServiceTest {
    @Test
    public void testGetOrder() {}
}
"""
    files = [
        ("src/main/java/OrderService.java", source),
        ("src/test/java/OrderServiceTest.java", test_source),
    ]
    summary = java_adapter.summarize_sources(files)

    assert summary["language"] == "java"
    assert summary["file_count"] == 2
    assert "com.example.model" in summary["packages"]
    assert "class OrderService" in summary["types"]
    assert "getOrder" in summary["methods"]
    assert "src/test/java/OrderServiceTest.java" in summary["test_files"]


def test_java_run_static_analysis_end_to_end(java_adapter, tmp_path):
    java_source = """package com.test;
public class TestApp {
    public void run() {
        try {
            // do work
        } catch (Exception e) {}
    }
}
"""
    findings, obs, tool_results = asyncio.run(
        java_adapter.run_static_analysis([("src/TestApp.java", java_source)], tmp_path)
    )

    tool_names = {t["tool"] for t in tool_results}
    assert "checkstyle" in tool_names
    assert "pmd" in tool_names
    assert "spotbugs" in tool_names
    assert "java_safety_analyzer" in tool_names

    # Built-in analyzer must catch the empty catch block
    assert any("JAVA-ERR-EMPTY-CATCH" in f.title for f in findings)
    assert len(obs) >= 1
