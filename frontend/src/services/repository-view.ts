import type { GitHubRepository } from './github';

export type RepositoryVisibility = 'all' | 'public' | 'private';
export type RepositoryOrder = 'updated' | 'name';

export function filterRepositories(
  repositories: GitHubRepository[],
  { query, visibility, language, order }: { query: string; visibility: RepositoryVisibility; language: string; order: RepositoryOrder },
) {
  const term = query.trim().toLowerCase();
  return repositories.filter((repo) => {
    const isPublic = repo.visibility === 'public';
    const matchesVisibility = visibility === 'all' || (visibility === 'public' ? isPublic : !isPublic);
    return matchesVisibility && (!language || repo.language === language) && repo.full_name.toLowerCase().includes(term);
  }).sort((first, second) => {
    if (order === 'updated') {
      const dateOrder = (Date.parse(second.updated_at) || 0) - (Date.parse(first.updated_at) || 0);
      if (dateOrder) return dateOrder;
    }
    return first.name.localeCompare(second.name) || first.full_name.localeCompare(second.full_name);
  });
}
