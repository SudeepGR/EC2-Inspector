# Optional dashboard tests

These run the dashboard page in a simulated browser (jsdom) with made-up data and
check filters, sorting, the ring, chips, keyboard shortcuts, copy buttons, themes
and the detail panel. They need Node.js; the app itself does not.

```bash
cd ui-tests
npm install
npm test
```
