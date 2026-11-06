// Use the shared isolated Edge profile and temporary test database.
process.env.VEST_SMOKE_STRATEGY_ONLY='1';
await import('./browser-smoke.mjs');
