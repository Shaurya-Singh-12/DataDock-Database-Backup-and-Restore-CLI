-- Runs once, the first time the MariaDB demo container starts.
USE demo;

CREATE TABLE students (
    id    INT AUTO_INCREMENT PRIMARY KEY,
    name  VARCHAR(100) NOT NULL,
    grade INT NOT NULL
) ENGINE=InnoDB;

INSERT INTO students (name, grade) VALUES
    ('Alice', 90),
    ('Bob',   85),
    ('Cara',  78);

CREATE TABLE courses (
    id    INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(100) NOT NULL
) ENGINE=InnoDB;

INSERT INTO courses (title) VALUES
    ('Databases'),
    ('Operating Systems');

CREATE VIEW top_students AS
    SELECT name FROM students WHERE grade >= 85;
