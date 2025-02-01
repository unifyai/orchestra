DROP FUNCTION IF EXISTS restore_order_text(jsonb);

CREATE OR REPLACE FUNCTION restore_order_text(input jsonb)
RETURNS json
LANGUAGE plpgsql IMMUTABLE AS
$$
DECLARE
    result text;
    rec record;
    elem record;
BEGIN
    -- If the input is a JSON object, rebuild it in the order specified by the order index.
    IF jsonb_typeof(input) = 'object' THEN
        result := '{';
        FOR rec IN
            SELECT key, value->1 AS val, (value->>0)::int as ord
            FROM jsonb_each(input)
            ORDER BY ord
        LOOP
            IF result <> '{' THEN
                result := result || ',';
            END IF;
            -- Use to_json(rec.key) to get the key quoted with double quotes.
            result := result || to_json(rec.key)::text || ':' || restore_order_text(rec.val)::text;
        END LOOP;
        result := result || '}';
        RETURN result::json;

    -- If the input is an array, process each element recursively.
    ELSIF jsonb_typeof(input) = 'array' THEN
        result := '[';
        FOR elem IN
            SELECT value AS val
            FROM jsonb_array_elements(input)
        LOOP
            IF result <> '[' THEN
                result := result || ',';
            END IF;
            result := result || restore_order_text(elem.val)::text;
        END LOOP;
        result := result || ']';
        RETURN result::json;

    -- For scalar values, simply return the JSON representation.
    ELSE
        RETURN to_json(input);
    END IF;
END;
$$;
