# Errorta Browser E2E

Playwright runs the real Vite app against a mocked sidecar at
`http://127.0.0.1:8770`. The mock is intentionally thin: it covers the browser
client contracts needed for navigation, Judge, Knowledge, Watch, and Rooms
without requiring a live Python sidecar, AIAR server, model runtime, or corpus.

Run:

```sh
npm run test:e2e
npm run test:e2e:a11y
```

The a11y command is a focused grep over tests tagged `@a11y`; the full e2e gate
also includes those tests.
