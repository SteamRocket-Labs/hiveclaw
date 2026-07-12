import fs from 'node:fs';
import path from 'node:path';
import { gzipSync } from 'node:zlib';

const MAX_AGENT_DETAIL_BYTES = 380_000;
const MAX_AGENT_DETAIL_GZIP_BYTES = 115_000;
const MAX_VENDOR_BYTES = 620_000;
const MAX_VENDOR_GZIP_BYTES = 200_000;
const manifestRelativePath = 'dist/.vite/manifest.json';
const manifestPath = path.resolve(manifestRelativePath);

if (!fs.existsSync(manifestPath)) {
  throw new Error(`AgentDetail bundle gate requires ${manifestRelativePath}; run vite build with manifest enabled.`);
}

const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const entry = Object.entries(manifest).find(([, chunk]) => (
  chunk.name === 'AgentDetail' && chunk.isDynamicEntry === true
));
if (!entry) {
  throw new Error('AgentDetail bundle gate could not find the AgentDetail dynamic entry in the Vite manifest.');
}

const [, chunk] = entry;
const chunkPath = path.resolve('dist', chunk.file);
const source = fs.readFileSync(chunkPath);
const bytes = source.byteLength;
const gzipBytes = gzipSync(source).byteLength;
const evidence = {
  schema: 'hive.frontend_bundle_budget.v1',
  route: '/agents/:id',
  source: 'src/pages/AgentDetail.tsx',
  chunk: chunk.file,
  bytes,
  gzip_bytes: gzipBytes,
  max_bytes: MAX_AGENT_DETAIL_BYTES,
  max_gzip_bytes: MAX_AGENT_DETAIL_GZIP_BYTES,
};
const vendorEntry = Object.entries(manifest).find(([, candidate]) => candidate.name === 'vendor');
if (!vendorEntry) {
  throw new Error('Frontend bundle gate could not find the shared vendor chunk in the Vite manifest.');
}
const [, vendorChunk] = vendorEntry;
const vendorSource = fs.readFileSync(path.resolve('dist', vendorChunk.file));
const vendorBytes = vendorSource.byteLength;
const vendorGzipBytes = gzipSync(vendorSource).byteLength;
const vendorEvidence = {
  schema: 'hive.frontend_bundle_budget.v1',
  route: 'shared',
  source: 'node_modules',
  chunk: vendorChunk.file,
  bytes: vendorBytes,
  gzip_bytes: vendorGzipBytes,
  max_bytes: MAX_VENDOR_BYTES,
  max_gzip_bytes: MAX_VENDOR_GZIP_BYTES,
};

fs.mkdirSync(path.resolve('dist/evidence'), { recursive: true });
fs.writeFileSync(
  path.resolve('dist/evidence/agent-detail-bundle.json'),
  `${JSON.stringify(evidence, null, 2)}\n`,
);
fs.writeFileSync(
  path.resolve('dist/evidence/frontend-bundle-budgets.json'),
  `${JSON.stringify({ schema: 'hive.frontend_bundle_budgets.v1', budgets: [evidence, vendorEvidence] }, null, 2)}\n`,
);

if (bytes > MAX_AGENT_DETAIL_BYTES || gzipBytes > MAX_AGENT_DETAIL_GZIP_BYTES) {
  throw new Error(
    `AgentDetail route chunk exceeds budget: ${bytes}/${MAX_AGENT_DETAIL_BYTES} bytes, `
      + `${gzipBytes}/${MAX_AGENT_DETAIL_GZIP_BYTES} gzip bytes.`,
  );
}
if (vendorBytes > MAX_VENDOR_BYTES || vendorGzipBytes > MAX_VENDOR_GZIP_BYTES) {
  throw new Error(
    `Shared vendor chunk exceeds budget: ${vendorBytes}/${MAX_VENDOR_BYTES} bytes, `
      + `${vendorGzipBytes}/${MAX_VENDOR_GZIP_BYTES} gzip bytes.`,
  );
}

console.log(
  `AgentDetail bundle budget passed: ${bytes}/${MAX_AGENT_DETAIL_BYTES} bytes, `
    + `${gzipBytes}/${MAX_AGENT_DETAIL_GZIP_BYTES} gzip bytes.`,
);
console.log(
  `Shared vendor bundle budget passed: ${vendorBytes}/${MAX_VENDOR_BYTES} bytes, `
    + `${vendorGzipBytes}/${MAX_VENDOR_GZIP_BYTES} gzip bytes.`,
);
