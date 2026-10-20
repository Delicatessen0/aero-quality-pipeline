-- AeroQ — example analyst queries against the star schema.
-- Written in ANSI SQL so they run on SQL Server, Oracle, Databricks SQL and SQLite
-- (swap LIMIT for TOP / FETCH FIRST as needed).

-- 1. First-pass yield by program and work center
SELECT w.program,
       wc.work_center_name,
       COUNT(*)                                              AS work_orders,
       AVG(CASE WHEN w.first_pass THEN 1.0 ELSE 0.0 END)     AS fpy
FROM fact_work_order w
JOIN dim_work_center wc ON wc.work_center_key = w.work_center_key
GROUP BY w.program, wc.work_center_name
ORDER BY fpy;

-- 2. Monthly defect rate by material (trend)
SELECT d.year, d.month, p.material,
       COUNT(*)                                              AS inspections,
       SUM(CASE WHEN i.passed THEN 0 ELSE 1 END) * 1.0 / COUNT(*) AS defect_rate
FROM fact_inspection i
JOIN fact_work_order w ON w.work_order_id = i.work_order_id
JOIN dim_part p        ON p.part_key = w.part_key
JOIN dim_date d        ON d.date_key = i.inspected_date_key
GROUP BY d.year, d.month, p.material
ORDER BY d.year, d.month, p.material;

-- 3. Critical nonconformances still awaiting scrap/rework decision downstream
SELECT i.inspection_id, w.work_order_id, w.program, p.part_number,
       i.defect_desc, i.disposition, i.inspected_at
FROM fact_inspection i
JOIN fact_work_order w ON w.work_order_id = i.work_order_id
JOIN dim_part p        ON p.part_key = w.part_key
WHERE i.severity = 'CRITICAL'
ORDER BY i.inspected_at DESC;

-- 4. Cure-cycle summary per autoclave work order (feeds the anomaly model)
SELECT work_order_id,
       AVG(CASE WHEN sensor = 'autoclave_temp_c'       THEN value END) AS temp_mean,
       MAX(CASE WHEN sensor = 'autoclave_temp_c'       THEN value END) AS temp_max,
       AVG(CASE WHEN sensor = 'autoclave_pressure_psi' THEN value END) AS pressure_mean
FROM fact_sensor_reading
GROUP BY work_order_id;

-- 5. Data-quality trend: failing rules per pipeline run
SELECT run_id, run_at,
       SUM(CASE WHEN NOT passed AND severity = 'error' THEN 1 ELSE 0 END) AS errors,
       SUM(CASE WHEN NOT passed AND severity = 'warn'  THEN 1 ELSE 0 END) AS warnings
FROM dq_result
GROUP BY run_id, run_at
ORDER BY run_at DESC;
