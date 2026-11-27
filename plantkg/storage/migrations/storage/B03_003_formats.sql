CREATE TABLE storage_formats (
    kind TEXT NOT NULL,
    format_version INTEGER NOT NULL,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (kind, format_version)
);
INSERT INTO storage_formats VALUES ('graph', 1, 'Oxigraph named graph with source and triple checksums', CURRENT_TIMESTAMP);
INSERT INTO storage_formats VALUES ('series', 1, 'Parquet bundle and record metadata', CURRENT_TIMESTAMP);
