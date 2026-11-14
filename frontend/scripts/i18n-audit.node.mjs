import assert from 'node:assert/strict';
import test from 'node:test';

import {
  auditCatalogs,
  auditSourceText,
  classifyDynamicCall,
  duplicateJsonKeys,
  flattenCatalog,
} from './i18n-audit.mjs';

test('flattens nested catalogs without treating arrays as namespaces', () => {
  assert.deepEqual(
    [...flattenCatalog({ common: { save: 'Save' }, choices: ['one', 'two'] }).entries()],
    [
      ['choices', ['one', 'two']],
      ['common.save', 'Save'],
    ],
  );
});

test('reports duplicate JSON object paths before JSON.parse can discard them', () => {
  assert.deepEqual(
    duplicateJsonKeys(
      'fixture.json',
      '{"agent":{"extensions":{"first":"one"},"extensions":{"second":"two"}}}',
    ),
    [
      {
        path: 'agent.extensions',
        firstLine: 1,
        duplicateLine: 1,
      },
    ],
  );
});

test('extracts literal translation keys and reports missing locale sides', () => {
  const source = `
    import { useTranslation } from 'react-i18next';
    export function Card() {
      const { t } = useTranslation();
      return <>{t('card.title')}{t(\`card.subtitle\`)}</>;
    }
  `;
  const sourceAudit = auditSourceText('src/Card.tsx', source);
  const catalogAudit = auditCatalogs(
    new Map([
      ['card.title', 'Title'],
      ['card.subtitle', 'Subtitle'],
    ]),
    new Map([['card.title', '标题']]),
    sourceAudit.staticCalls,
  );

  assert.deepEqual(sourceAudit.staticCalls.map((call) => call.key), ['card.title', 'card.subtitle']);
  assert.deepEqual(catalogAudit.missingEnglish, []);
  assert.deepEqual(catalogAudit.missingChinese, ['card.subtitle']);
  assert.deepEqual(catalogAudit.missingBoth, []);
});

test('ignores unrelated functions named t when react-i18next is not imported', () => {
  const sourceAudit = auditSourceText(
    'src/math.ts',
    `const t = (value) => value; export const result = t('not.translation');`,
  );

  assert.deepEqual(sourceAudit.staticCalls, []);
  assert.deepEqual(sourceAudit.dynamicCalls, []);
});

test('audits only explicitly registered translation wrapper callees', () => {
  const source = `
    import { useTranslation } from 'react-i18next';
    export function summary(translate) {
      return translate('card.title', 'Title');
    }
  `;
  const wrapperRules = [
    {
      source: 'src/Card.tsx',
      callee: 'translate',
      reason: 'The helper receives the react-i18next translator from its only production caller.',
    },
  ];

  assert.deepEqual(
    auditSourceText('src/Card.tsx', source, wrapperRules).staticCalls.map((call) => call.key),
    ['card.title'],
  );
  assert.deepEqual(auditSourceText('src/Other.tsx', source, wrapperRules).staticCalls, []);
});

test('detects Chinese literal and defaultValue fallbacks through the AST', () => {
  const source = `
    import { useTranslation } from 'react-i18next';
    export function Card() {
      const { t } = useTranslation();
      return <>
        {t('card.first', '中文默认')}
        {t('card.second', { defaultValue: '另一个默认' })}
        {t('card.third', 'English fallback')}
      </>;
    }
  `;

  const sourceAudit = auditSourceText('src/Card.tsx', source);

  assert.deepEqual(
    sourceAudit.chineseDefaults.map((entry) => entry.key),
    ['card.first', 'card.second'],
  );
});

test('expands finite dynamic templates against both catalogs', () => {
  const english = new Map([
    ['status.ready', 'Ready'],
    ['status.failed', 'Failed'],
  ]);
  const chinese = new Map([
    ['status.ready', '就绪'],
    ['status.failed', '失败'],
  ]);
  const dynamic = {
    expression: '`status.${state}`',
    kind: 'template',
    prefix: 'status.',
    suffix: '',
    hasFallback: false,
  };

  assert.deepEqual(classifyDynamicCall(dynamic, english, chinese, []), {
    strategy: 'catalog_pattern',
    matchedKeys: ['status.failed', 'status.ready'],
  });
});

test('requires an explicit rule for an unresolved dynamic key without fallback', () => {
  const dynamic = {
    source: 'src/Card.tsx',
    expression: 'labelKey',
    kind: 'expression',
    hasFallback: false,
  };

  assert.deepEqual(classifyDynamicCall(dynamic, new Map(), new Map(), []), {
    strategy: 'unresolved',
    matchedKeys: [],
  });
  assert.deepEqual(
    classifyDynamicCall(dynamic, new Map(), new Map(), [
      {
        source: 'src/Card.tsx',
        expression: 'labelKey',
        reason: 'The typed card registry owns the complete translation-key set.',
      },
    ]),
    {
      strategy: 'explicit_rule',
      matchedKeys: [],
      reason: 'The typed card registry owns the complete translation-key set.',
    },
  );
});

test('does not let a runtime fallback bypass the explicit dynamic-key rule table', () => {
  const result = classifyDynamicCall(
    {
      source: 'src/Card.tsx',
      expression: 'row.labelKey',
      kind: 'expression',
      hasFallback: true,
    },
    new Map(),
    new Map(),
    [],
  );

  assert.deepEqual(result, {
    strategy: 'unresolved',
    matchedKeys: [],
  });
});
