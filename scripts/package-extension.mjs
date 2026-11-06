import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { mkdirSync } from 'node:fs';
const root = fileURLToPath(new URL('../', import.meta.url));
mkdirSync(new URL('../public/', import.meta.url), { recursive: true });
const result = spawnSync('python', ['-c', "import pathlib, zipfile; root=pathlib.Path.cwd(); files=sorted((root/'extension').glob('*')); out=root/'public'/'vest-browser-bridge.zip'; z=zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED); [z.write(p,p.name) for p in files if p.is_file()]; z.close(); print('Extension packaged: public/vest-browser-bridge.zip')"], { cwd: root, stdio: 'inherit' });
process.exitCode = result.status || 0;
