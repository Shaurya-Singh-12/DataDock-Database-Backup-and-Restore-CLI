-- Runs once, the first time the demo database container starts.

CREATE TABLE students (
    id    SERIAL PRIMARY KEY,
    name  TEXT NOT NULL,
    grade INT  NOT NULL
);

INSERT INTO students (name, grade) VALUES
    ('Alice', 90),
    ('Bob',   85),
    ('Cara',  78);

CREATE TABLE courses (
    id    SERIAL PRIMARY KEY,
    title TEXT NOT NULL
);

INSERT INTO courses (title) VALUES
    ('Databases'),
    ('Operating Systems');
