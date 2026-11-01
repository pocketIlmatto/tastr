// Searches in the browser on a hosted server (TAS-49): this worker holds sql.js (SQLite built as WebAssembly, MIT,
// served by the Tastr server at /sqljs/) and a copy of the person's own library (GET /api/query/library). Running
// here rather than on the page means a slow query can't freeze the tab: the page stops it by ending the worker.
// In: {base, lib: ArrayBuffer} loads; {sql, max} runs one statement. Out: {ready} | {columns, rows} | {error, where}.

let SQL = null, db = null;

self.onmessage = async (e) => {
  const m = e.data;
  if (m.lib) {
    try {
      if (!SQL) {
        importScripts(m.base + "sql-wasm.js");
        SQL = await self.initSqlJs({ locateFile: (f) => m.base + f });
      }
    } catch (err) {
      self.postMessage({ error: String(err && err.message || err), where: "engine" });
      return;
    }
    try {
      if (db) db.close();
      db = new SQL.Database(new Uint8Array(m.lib));
      db.exec("PRAGMA query_only = ON");  // it's only a copy, but a search never changes anything
      self.postMessage({ ready: true });
    } catch (err) {
      self.postMessage({ error: String(err && err.message || err), where: "library" });
    }
    return;
  }
  let stmt = null;
  try {
    stmt = db.prepare(m.sql);
    const columns = stmt.getColumnNames(), rows = [];
    while (rows.length < m.max && stmt.step()) rows.push(stmt.get());
    self.postMessage({ columns, rows });
  } catch (err) {
    self.postMessage({ error: String(err && err.message || err), where: "query" });
  } finally {
    if (stmt) stmt.free();
  }
};
