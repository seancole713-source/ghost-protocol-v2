-- Fix legacy WOLF rows mislabeled asset_type='crypto' (pre WOLF-only pivot).
-- Safe to run on Railway Postgres. Reversible via ROLLBACK before COMMIT.
--
-- Usage (Railway psql or query tab):
--   Run steps 1-3 first. If counts look right, run step 4 in a transaction.

-- Step 1: Count affected rows
SELECT COUNT(*) AS mislabeled_wolf_crypto
FROM predictions
WHERE symbol = 'WOLF' AND asset_type = 'crypto';

-- Step 2: Sample rows (sanity check — should be old/expired FLAT/DOWN junk)
SELECT id, predicted_at, direction, outcome, asset_type, entry_price
FROM predictions
WHERE symbol = 'WOLF' AND asset_type = 'crypto'
ORDER BY predicted_at DESC
LIMIT 10;

-- Step 3: Latest affected timestamp
SELECT MAX(predicted_at) AS latest_mislabeled_ts
FROM predictions
WHERE symbol = 'WOLF' AND asset_type = 'crypto';

-- Step 4: Apply fix (review steps 1-3, then COMMIT or ROLLBACK)
BEGIN;
UPDATE predictions
SET asset_type = 'stock'
WHERE symbol = 'WOLF' AND asset_type = 'crypto';
-- SELECT COUNT(*) FROM predictions WHERE symbol = 'WOLF' AND asset_type = 'crypto';
-- Expected: 0 before COMMIT
COMMIT;

-- Step 5: Verify clean
SELECT COUNT(*) AS remaining_mislabeled
FROM predictions
WHERE symbol = 'WOLF' AND asset_type = 'crypto';
