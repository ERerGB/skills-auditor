import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash, randomBytes } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import worker from "./worker.mjs";

test("private reads, immutable history and generation-checked rollback", async () => {
  const db = new DatabaseSync(":memory:");
  db.exec(readFileSync(new URL("./schema.sql", import.meta.url), "utf8"));
  const env = { DB: { prepare: sql => ({ bind: (...args) => ({ first: () => db.prepare(sql).get(...args) }) }) } };
  const token = randomBytes(32).toString("hex");
  const digest = value => createHash("sha256").update(value).digest("hex");
  db.prepare("INSERT INTO readers VALUES (?,?,?)").run(digest(token),"example","client");
  const request = (path, auth = token) => worker.fetch(new Request("https://example.test/v1/" + path,
    {headers: auth ? { Authorization: "Bearer " + auth } : {}}), env);
  for (const path of ["me", "example/current", "example/releases/" + "0".repeat(64) + "/0"]) {
    assert.equal((await request(path, "")).status, 401);
    assert.equal((await request(path, "0".repeat(64))).status, 401);
  }
  assert.equal((await request("wrong/current")).status, 403);
  assert.deepEqual(await (await request("me")).json(), {workspace: "example"});
  const create = text => {
    const bytes = Buffer.from(text), pin = digest(bytes);
    db.prepare("INSERT INTO chunks VALUES (?,?,0,?)").run("example", pin, bytes);
    assert.throws(() => db.prepare("INSERT INTO releases VALUES (?,?,2,?)").run("example",pin,bytes.length));
    db.prepare("INSERT INTO releases VALUES (?,?,1,?)").run("example",pin,bytes.length);
    return pin;
  };
  const first = create("first"), second = create("next");
  const publish = (gen, pin) => db.prepare("INSERT INTO promotions VALUES (?,?,?)").run("example",gen,pin);
  assert.equal((await request("example/releases/" + first)).status,404);
  publish(1,first);
  publish(2,second);
  assert.throws(() => publish(2,first));
  publish(3,first);
  assert.deepEqual(await (await request("example/current")).json(), {generation:3,digest:first});
  assert.equal(await (await request("example/releases/" + second + "/0")).text(),"next");
  assert.equal((await request("wrong/releases/" + second + "/0")).status,403);
  assert.throws(() => db.exec("UPDATE chunks SET part=1"));
  db.exec("DELETE FROM readers");
  assert.equal((await request("me")).status,401);
});
