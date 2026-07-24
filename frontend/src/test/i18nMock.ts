type TranslationCatalog = Record<string, unknown>;
type TranslationOptions = Record<string, unknown>;

function catalogValue(catalog: TranslationCatalog, key: string): unknown {
  return key.split('.').reduce<unknown>((current, segment) => {
    if (!current || typeof current !== 'object' || Array.isArray(current)) {
      return undefined;
    }
    return (current as Record<string, unknown>)[segment];
  }, catalog);
}

function optionsFrom(
  fallbackOrOptions?: string | TranslationOptions,
  interpolationOptions?: TranslationOptions,
): TranslationOptions {
  if (typeof fallbackOrOptions === 'object' && fallbackOrOptions !== null) {
    return fallbackOrOptions;
  }
  return interpolationOptions ?? {};
}

export function translateFromCatalog(
  catalog: TranslationCatalog,
  key: string,
  fallbackOrOptions?: string | TranslationOptions,
  interpolationOptions?: TranslationOptions,
): string {
  const options = optionsFrom(fallbackOrOptions, interpolationOptions);
  const count = options.count;
  const pluralKey =
    typeof count === 'number' ? `${key}_${count === 1 ? 'one' : 'other'}` : undefined;
  const translated =
    (pluralKey ? catalogValue(catalog, pluralKey) : undefined) ?? catalogValue(catalog, key);
  const defaultValue =
    typeof fallbackOrOptions === 'string'
      ? fallbackOrOptions
      : typeof options.defaultValue === 'string'
        ? options.defaultValue
        : undefined;
  const template = typeof translated === 'string' ? translated : (defaultValue ?? key);

  return template.replace(/\{\{\s*([^},\s]+)[^}]*\}\}/g, (_match, name: string) => {
    const replacement = options[name];
    return replacement === undefined || replacement === null ? '' : String(replacement);
  });
}
