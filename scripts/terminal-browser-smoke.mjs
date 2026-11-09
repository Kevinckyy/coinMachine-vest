// Reuse the isolated Edge profile and temporary database; no live Vest access.
process.env.VEST_SMOKE_FAILURE_ONLY='1';
await import('./browser-smoke.mjs');
