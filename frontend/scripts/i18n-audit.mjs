import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import ts from 'typescript';

import {
  DYNAMIC_KEY_RULES,
  TRANSLATION_CALLEE_RULES,
} from './i18n-audit.config.mjs';

const CHINESE_TEXT = /\p{Script=Han}/u;
const SOURCE_EXTENSION = /\.[cm]?[jt]sx?$/;
const TEST_FILE = /\.(?:test|spec)\.[cm]?[jt]sx?$/;

function sorted(values) {
  return [...values].sort((left, right) => left.localeCompare(right));
}

function stableHash(value) {
  return createHash('sha256').update(JSON.stringify(value)).digest('hex');
}

function toPosix(value) {
  return value.split(path.sep).join('/');
}

export function flattenCatalog(value, prefix = '') {
  const entries = [];
  for (const [key, child] of Object.entries(value)) {
    const fullKey = prefix ? `${prefix}.${key}` : key;
    if (child && typeof child === 'object' && !Array.isArray(child)) {
      entries.push(...flattenCatalog(child, fullKey).entries());
    } else {
      entries.push([fullKey, child]);
    }
  }
  return new Map(entries.sort(([left], [right]) => left.localeCompare(right)));
}

export function duplicateJsonKeys(source, text) {
  const sourceFile = ts.parseJsonText(source, text);
  const duplicates = [];

  function visit(node, prefix = '') {
    if (!ts.isObjectLiteralExpression(node)) return;
    const seen = new Map();
    for (const property of node.properties) {
      if (!ts.isPropertyAssignment(property)) continue;
      const name = ts.isStringLiteralLike(property.name)
        ? property.name.text
        : property.name.getText(sourceFile);
      const fullPath = prefix ? `${prefix}.${name}` : name;
      const first = seen.get(name);
      if (first) {
        duplicates.push({
          path: fullPath,
          firstLine: sourceFile.getLineAndCharacterOfPosition(first.getStart(sourceFile)).line + 1,
          duplicateLine:
            sourceFile.getLineAndCharacterOfPosition(property.getStart(sourceFile)).line + 1,
        });
      } else {
        seen.set(name, property);
      }
      visit(property.initializer, fullPath);
    }
  }

  const rootExpression = sourceFile.statements[0]?.expression;
  if (rootExpression) visit(rootExpression);
  return duplicates;
}

function fallbackLiteral(call, sourceFile) {
  const fallback = call.arguments[1];
  if (!fallback) return null;
  if (ts.isStringLiteralLike(fallback)) return fallback.text;
  if (!ts.isObjectLiteralExpression(fallback)) return null;
  for (const property of fallback.properties) {
    if (!ts.isPropertyAssignment(property)) continue;
    const name = property.name.getText(sourceFile).replace(/^['"]|['"]$/g, '');
    if (name === 'defaultValue' && ts.isStringLiteralLike(property.initializer)) {
      return property.initializer.text;
    }
  }
  return null;
}

function literalAlternatives(node) {
  if (ts.isStringLiteralLike(node)) return [node.text];
  if (ts.isParenthesizedExpression(node)) return literalAlternatives(node.expression);
  if (ts.isConditionalExpression(node)) {
    const whenTrue = literalAlternatives(node.whenTrue);
    const whenFalse = literalAlternatives(node.whenFalse);
    return whenTrue && whenFalse ? [...whenTrue, ...whenFalse] : null;
  }
  return null;
}

function dynamicShape(node, sourceFile) {
  if (ts.isTemplateExpression(node)) {
    return {
      kind: 'template',
      prefix: node.head.text,
      suffix: node.templateSpans.at(-1)?.literal.text ?? '',
    };
  }
  const alternatives = literalAlternatives(node);
  if (alternatives) {
    return { kind: 'finite_alternatives', alternatives: sorted(new Set(alternatives)) };
  }
  return { kind: 'expression' };
}

function isTranslationSource(sourceFile) {
  return sourceFile.statements.some(
    (statement) =>
      ts.isImportDeclaration(statement) &&
      ts.isStringLiteral(statement.moduleSpecifier) &&
      statement.moduleSpecifier.text === 'react-i18next',
  );
}

export function auditSourceText(source, text, calleeRules = TRANSLATION_CALLEE_RULES) {
  const sourceFile = ts.createSourceFile(
    source,
    text,
    ts.ScriptTarget.Latest,
    true,
    source.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  if (!isTranslationSource(sourceFile)) {
    return { staticCalls: [], dynamicCalls: [], chineseDefaults: [] };
  }
  const translationCallees = new Set([
    't',
    ...calleeRules
      .filter((rule) => rule.source === source)
      .map((rule) => rule.callee),
  ]);

  const staticCalls = [];
  const dynamicCalls = [];
  const chineseDefaults = [];

  function visit(node) {
    if (
      ts.isCallExpression(node) &&
      ts.isIdentifier(node.expression) &&
      translationCallees.has(node.expression.text) &&
      node.arguments.length > 0
    ) {
      const keyNode = node.arguments[0];
      const position = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
      const location = {
        source,
        line: position.line + 1,
        column: position.character + 1,
      };
      const fallback = fallbackLiteral(node, sourceFile);
      if (ts.isStringLiteralLike(keyNode)) {
        const call = { ...location, key: keyNode.text };
        staticCalls.push(call);
        if (fallback && CHINESE_TEXT.test(fallback)) {
          chineseDefaults.push({ ...call, fallback });
        }
      } else {
        dynamicCalls.push({
          ...location,
          expression: keyNode.getText(sourceFile),
          hasFallback: fallback !== null || node.arguments.length > 1,
          ...dynamicShape(keyNode, sourceFile),
        });
      }
    }
    ts.forEachChild(node, visit);
  }

  visit(sourceFile);
  return { staticCalls, dynamicCalls, chineseDefaults };
}

export function auditCatalogs(english, chinese, staticCalls) {
  const staticKeys = sorted(new Set(staticCalls.map((call) => call.key)));
  const missingBoth = [];
  const missingEnglish = [];
  const missingChinese = [];
  for (const key of staticKeys) {
    const inEnglish = english.has(key);
    const inChinese = chinese.has(key);
    if (!inEnglish && !inChinese) missingBoth.push(key);
    else if (!inEnglish) missingEnglish.push(key);
    else if (!inChinese) missingChinese.push(key);
  }

  return {
    staticKeys,
    missingBoth,
    missingEnglish,
    missingChinese,
    catalogOnlyEnglish: sorted([...english.keys()].filter((key) => !chinese.has(key))),
    catalogOnlyChinese: sorted([...chinese.keys()].filter((key) => !english.has(key))),
  };
}

function matchingPatternKeys(call, catalog) {
  return sorted(
    [...catalog.keys()].filter(
      (key) => key.startsWith(call.prefix) && key.endsWith(call.suffix),
    ),
  );
}

export function classifyDynamicCall(call, english, chinese, rules = DYNAMIC_KEY_RULES) {
  if (call.kind === 'template' && call.prefix) {
    const englishMatches = matchingPatternKeys(call, english);
    const chineseMatches = matchingPatternKeys(call, chinese);
    if (
      englishMatches.length > 0 &&
      JSON.stringify(englishMatches) === JSON.stringify(chineseMatches)
    ) {
      return { strategy: 'catalog_pattern', matchedKeys: englishMatches };
    }
  }

  if (call.kind === 'finite_alternatives') {
    const alternatives = sorted(call.alternatives);
    if (
      alternatives.length > 0 &&
      alternatives.every((key) => english.has(key) && chinese.has(key))
    ) {
      return { strategy: 'finite_alternatives', matchedKeys: alternatives };
    }
  }

  const explicitRule = rules.find(
    (rule) => rule.source === call.source && rule.expression === call.expression,
  );
  if (explicitRule) {
    return {
      strategy: 'explicit_rule',
      matchedKeys: [],
      reason: explicitRule.reason,
    };
  }

  return { strategy: 'unresolved', matchedKeys: [] };
}

function sourceFiles(rootDirectory) {
  const files = [];
  function walk(directory) {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        walk(absolute);
      } else if (
        SOURCE_EXTENSION.test(entry.name) &&
        !TEST_FILE.test(entry.name) &&
        !entry.name.endsWith('.d.ts')
      ) {
        files.push(absolute);
      }
    }
  }
  walk(path.join(rootDirectory, 'src'));
  return files.sort((left, right) => left.localeCompare(right));
}

export function buildInventory(rootDirectory, rules = DYNAMIC_KEY_RULES) {
  const englishPath = path.join(rootDirectory, 'src/i18n/en.json');
  const chinesePath = path.join(rootDirectory, 'src/i18n/zh.json');
  const englishText = fs.readFileSync(englishPath, 'utf8');
  const chineseText = fs.readFileSync(chinesePath, 'utf8');
  const english = flattenCatalog(JSON.parse(englishText));
  const chinese = flattenCatalog(JSON.parse(chineseText));
  const duplicateCatalogKeys = [
    ...duplicateJsonKeys('src/i18n/en.json', englishText).map((entry) => ({
      locale: 'en',
      ...entry,
    })),
    ...duplicateJsonKeys('src/i18n/zh.json', chineseText).map((entry) => ({
      locale: 'zh',
      ...entry,
    })),
  ];
  const allStaticCalls = [];
  const allDynamicCalls = [];
  const allChineseDefaults = [];
  const files = sourceFiles(rootDirectory);

  for (const absolute of files) {
    const source = toPosix(path.relative(rootDirectory, absolute));
    const audit = auditSourceText(source, fs.readFileSync(absolute, 'utf8'));
    allStaticCalls.push(...audit.staticCalls);
    allDynamicCalls.push(...audit.dynamicCalls);
    allChineseDefaults.push(...audit.chineseDefaults);
  }

  const catalogAudit = auditCatalogs(english, chinese, allStaticCalls);
  const dynamicCalls = allDynamicCalls.map((call) => ({
    ...call,
    ...classifyDynamicCall(call, english, chinese, rules),
  }));
  const unresolvedDynamic = dynamicCalls.filter((call) => call.strategy === 'unresolved');
  const strategyCounts = Object.fromEntries(
    sorted(new Set(dynamicCalls.map((call) => call.strategy))).map((strategy) => [
      strategy,
      dynamicCalls.filter((call) => call.strategy === strategy).length,
    ]),
  );

  const inventory = {
    schemaVersion: 1,
    sourceFiles: files.length,
    englishCatalogKeys: english.size,
    chineseCatalogKeys: chinese.size,
    staticCallSites: allStaticCalls.length,
    staticKeys: catalogAudit.staticKeys.length,
    dynamicCallSites: dynamicCalls.length,
    dynamicStrategies: strategyCounts,
    hashes: {
      staticKeys: stableHash(catalogAudit.staticKeys),
      dynamicCalls: stableHash(
        dynamicCalls.map(({ source, line, expression, strategy, matchedKeys }) => ({
          source,
          line,
          expression,
          strategy,
          matchedKeys,
        })),
      ),
      englishCatalog: stableHash([...english.entries()]),
      chineseCatalog: stableHash([...chinese.entries()]),
    },
    missingBoth: catalogAudit.missingBoth,
    missingEnglish: catalogAudit.missingEnglish,
    missingChinese: catalogAudit.missingChinese,
    catalogOnlyEnglish: catalogAudit.catalogOnlyEnglish,
    catalogOnlyChinese: catalogAudit.catalogOnlyChinese,
    duplicateCatalogKeys,
    chineseDefaults: allChineseDefaults,
    unresolvedDynamic,
  };

  return inventory;
}

export function inventoryFailures(inventory) {
  return {
    missingBoth: inventory.missingBoth.length,
    missingEnglish: inventory.missingEnglish.length,
    missingChinese: inventory.missingChinese.length,
    catalogOnlyEnglish: inventory.catalogOnlyEnglish.length,
    catalogOnlyChinese: inventory.catalogOnlyChinese.length,
    duplicateCatalogKeys: inventory.duplicateCatalogKeys.length,
    chineseDefaults: inventory.chineseDefaults.length,
    unresolvedDynamic: inventory.unresolvedDynamic.length,
  };
}

function hasFailures(failures) {
  return Object.values(failures).some((count) => count > 0);
}

function printHuman(inventory) {
  const failures = inventoryFailures(inventory);
  process.stdout.write(
    [
      `i18n inventory: ${inventory.sourceFiles} source files`,
      `catalog keys: en=${inventory.englishCatalogKeys} zh=${inventory.chineseCatalogKeys}`,
      `translation calls: static=${inventory.staticCallSites} (${inventory.staticKeys} unique) dynamic=${inventory.dynamicCallSites}`,
      `dynamic strategies: ${JSON.stringify(inventory.dynamicStrategies)}`,
      `gates: ${JSON.stringify(failures)}`,
      `inventory hashes: ${JSON.stringify(inventory.hashes)}`,
    ].join('\n') + '\n',
  );
  if (hasFailures(failures)) {
    for (const [name, count] of Object.entries(failures)) {
      if (count > 0) process.stdout.write(`- ${name}: ${count}\n`);
    }
  }
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : '';
if (invokedPath === fileURLToPath(import.meta.url)) {
  const rootDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const inventory = buildInventory(rootDirectory);
  const json = process.argv.includes('--json');
  const check = process.argv.includes('--check');
  if (json) process.stdout.write(`${JSON.stringify(inventory, null, 2)}\n`);
  else printHuman(inventory);
  if (check && hasFailures(inventoryFailures(inventory))) process.exitCode = 1;
}
