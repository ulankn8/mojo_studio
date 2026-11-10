-- Edit format of the project and track role of each file. NULL = before formats:
-- format toi, role from kind (video -> main, music -> music).
ALTER TABLE project ADD COLUMN format TEXT;
ALTER TABLE media ADD COLUMN role TEXT;
