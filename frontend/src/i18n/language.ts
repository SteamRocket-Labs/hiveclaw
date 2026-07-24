export function isChineseLanguage(language: string | undefined): boolean {
  return Boolean(language?.toLowerCase().startsWith('zh'));
}

export function nextInterfaceLanguage(language: string | undefined): 'en' | 'zh' {
  return isChineseLanguage(language) ? 'en' : 'zh';
}

export function htmlLanguage(language: string | undefined): 'en' | 'zh-CN' {
  return isChineseLanguage(language) ? 'zh-CN' : 'en';
}

export function syncDocumentLanguage(
  language: string | undefined,
  root: { lang: string } | null = typeof document === 'undefined' ? null : document.documentElement,
): void {
  if (root) {
    root.lang = htmlLanguage(language);
  }
}
