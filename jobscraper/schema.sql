-- 手动建库建表（等价于 python init_db.py 做的事）
-- 用法: mysql -u root -p < schema.sql

CREATE DATABASE IF NOT EXISTS `jobscraper`
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE `jobscraper`;

CREATE TABLE IF NOT EXISTS jobs (
    site          VARCHAR(16)  NOT NULL COMMENT '站点标识 boss/liepin/51job/zhilian',
    job_id        VARCHAR(96)  NOT NULL COMMENT '站内职位 ID',
    title         VARCHAR(255) NOT NULL,
    company       VARCHAR(255) DEFAULT NULL,
    city          VARCHAR(64)  DEFAULT NULL,
    district      VARCHAR(128) DEFAULT NULL,
    salary_raw    VARCHAR(64)  DEFAULT NULL COMMENT '原始薪资文本',
    salary_min    DECIMAL(6,1) DEFAULT NULL COMMENT '月薪下限 K',
    salary_max    DECIMAL(6,1) DEFAULT NULL COMMENT '月薪上限 K',
    salary_months TINYINT      DEFAULT NULL COMMENT '几薪',
    experience    VARCHAR(64)  DEFAULT NULL,
    education     VARCHAR(64)  DEFAULT NULL,
    skills        VARCHAR(512) DEFAULT NULL COMMENT '分号分隔',
    url           VARCHAR(1024) DEFAULT NULL,
    published_at  VARCHAR(64)  DEFAULT NULL,
    fetched_at    DATETIME     NOT NULL,
    raw           LONGTEXT     COMMENT '接口原始 JSON',
    PRIMARY KEY (site, job_id),
    KEY idx_city (city),
    KEY idx_company (company),
    KEY idx_salary (salary_min),
    KEY idx_fetched (fetched_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
