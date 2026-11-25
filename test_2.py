
import os, sys
import subprocess


def calculate_total(price, quantity):
    unused_variable = 100
    return price * quantity


def read_file(filename):
    with open(filename, "r") as f:
        return f.read()


def run_command(user_input):
    # Intentionally unsafe for Bandit testing
    subprocess.call("echo " + user_input, shell=True)


def weak_hash(password):
    import hashlib
    return hashlib.md5(password.encode()).hexdigest()


if __name__ == "__main__":
    print(calculate_total(10, 3))
    run_command("hello")
