CREATE TABLE readers (
  token_sha256 TEXT PRIMARY KEY NOT NULL CHECK(length(token_sha256) = 64),
  workspace TEXT NOT NULL,
  client TEXT NOT NULL,
  UNIQUE(workspace, client)
);

CREATE TABLE chunks (
  workspace TEXT NOT NULL, digest TEXT NOT NULL, part INTEGER NOT NULL CHECK(part >= 0),
  body BLOB NOT NULL CHECK(length(body) BETWEEN 1 AND 32768),
  PRIMARY KEY (workspace, digest, part)
);

CREATE TABLE releases (
  workspace TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest) = 64),
  parts INTEGER NOT NULL CHECK(parts BETWEEN 1 AND 2048),
  size INTEGER NOT NULL CHECK(size BETWEEN 1 AND 67108864),
  PRIMARY KEY (workspace, digest)
);

CREATE TRIGGER seal_release BEFORE INSERT ON releases BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM chunks WHERE workspace=NEW.workspace AND digest=NEW.digest
    GROUP BY workspace,digest HAVING COUNT(*)=NEW.parts AND MIN(part)=0 AND MAX(part)=NEW.parts-1 AND SUM(length(body))=NEW.size
  ) THEN RAISE(ABORT, 'incomplete release') END;
END;

CREATE TABLE promotions (
  workspace TEXT NOT NULL, generation INTEGER NOT NULL, digest TEXT NOT NULL,
  PRIMARY KEY (workspace, generation),
  FOREIGN KEY (workspace,digest) REFERENCES releases(workspace,digest)
);
CREATE TRIGGER advance_pointer BEFORE INSERT ON promotions BEGIN
  SELECT CASE WHEN NEW.generation != 1 + COALESCE(
    (SELECT MAX(generation) FROM promotions WHERE workspace=NEW.workspace), 0
  ) THEN RAISE(ABORT, 'stale current generation') END;
END;
CREATE TRIGGER reject_extra_chunk BEFORE INSERT ON chunks
WHEN EXISTS (SELECT 1 FROM releases WHERE workspace=NEW.workspace AND digest=NEW.digest)
BEGIN SELECT RAISE(ABORT,'sealed release'); END;
CREATE TRIGGER no_chunks_update BEFORE UPDATE ON chunks
BEGIN SELECT RAISE(ABORT,'immutable release history'); END;
CREATE TRIGGER no_chunks_delete BEFORE DELETE ON chunks
BEGIN SELECT RAISE(ABORT,'immutable release history'); END;
CREATE TRIGGER no_releases_update BEFORE UPDATE ON releases
BEGIN SELECT RAISE(ABORT,'immutable release history'); END;
CREATE TRIGGER no_releases_delete BEFORE DELETE ON releases
BEGIN SELECT RAISE(ABORT,'immutable release history'); END;
CREATE TRIGGER no_promotions_update BEFORE UPDATE ON promotions
BEGIN SELECT RAISE(ABORT,'immutable release history'); END;
CREATE TRIGGER no_promotions_delete BEFORE DELETE ON promotions
BEGIN SELECT RAISE(ABORT,'immutable release history'); END;
