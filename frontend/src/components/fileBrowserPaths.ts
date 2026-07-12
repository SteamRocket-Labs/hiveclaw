function normalizeSkillFolderName(value: string): string {
  const cleaned = value.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
  const withoutSkillFile = cleaned.replace(/\/SKILL\.md$/i, '');
  const withoutFlatMd = withoutSkillFile.replace(/\.md$/i, '');
  return withoutFlatMd
    .split('/')
    .filter(Boolean)
    .map((part) => part.trim().replace(/\s+/g, '-'))
    .filter(Boolean)
    .join('/');
}

export function buildNewSkillFilePath(currentPath: string, value: string): string {
  const folderName = normalizeSkillFolderName(value);
  const base = currentPath.trim().replace(/\/+$/g, '');
  return base ? `${base}/${folderName}/SKILL.md` : `${folderName}/SKILL.md`;
}
