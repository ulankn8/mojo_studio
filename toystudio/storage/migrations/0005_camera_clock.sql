-- Clock correction per camera (camera = the folder its files are in), from one pair of clips
-- the user marked as shot at the same time. Unix time of a clip = creation_time + offset_us.
CREATE TABLE camera_clock (
    camera_id TEXT PRIMARY KEY,
    offset_us INTEGER NOT NULL
);
