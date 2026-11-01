const { execFileSync } = require('node:child_process');
const { mkdirSync, writeFileSync, rmSync } = require('node:fs');
try {
  execFileSync(process.execPath, ['node_modules/typescript/bin/tsc', '--target', 'ES2022',
    '--lib', 'ES2022,DOM,DOM.Iterable', '--module', 'commonjs', '--moduleResolution', 'node',
    '--strict', '--esModuleInterop', '--skipLibCheck', '--outDir', '.test-build',
    'src/lib/api.ts', 'src/lib/eventStream.ts', 'src/lib/liveView.ts'], { stdio: 'inherit' });
  mkdirSync('.test-build', { recursive: true });
  writeFileSync('.test-build/package.json', '{"type":"commonjs"}');
  execFileSync(process.execPath, ['--test', 'tests/eventStream.test.cjs', 'tests/liveView.test.cjs'], { stdio: 'inherit' });
} finally { rmSync('.test-build', { recursive: true, force: true }); }
