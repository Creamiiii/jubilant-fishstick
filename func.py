from datetime import datetime


def insert_item(cursor, name: str) -> None:
    if name == '':
        raise Exception('Item name cannot be empty')
        return
    if not cursor:
        raise Exception('Cursor not found')
        return

    select_query = """
    SELECT COALESCE(MAX(id), -1) + 1 FROM items;
    """
    cursor.execute(select_query)
    new_id = cursor.fetchall()[0][0]

    insert_query = """
    INSERT INTO items (name, id)
    VALUES (%s, %s)
    """

    data = (name, new_id)
    cursor.execute(insert_query, data)


def wipe_items(cursor) -> None:
    if not cursor:
        raise Exception('Cursor not found')
        return
    
    query = """
    DELETE FROM items;
    """
    cursor.execute(query)


def insert_log(
    cursor,
    item: str,
    time: datetime,
    color: str,
    center_pixel_left_x: float,
    center_pixel_left_y: float,
    center_pixel_right_x: float,
    center_pixel_right_y: float,
    width: float,
    height: float,
    pos_x: float,
    pos_y: float,
    pos_z=None,
    confidence: str = '',
    notes=None,
) -> None:
    expected_types = {
        "item": (item, str),
        "time": (time, datetime),
        "color": (color, str),
        "center_pixel_left_x": (center_pixel_left_x, (int, float)),
        "center_pixel_left_y": (center_pixel_left_y, (int, float)),
        "center_pixel_right_x": (center_pixel_right_x, (int, float)),
        "center_pixel_right_y": (center_pixel_right_y, (int, float)),
        "width": (width, (int, float)),
        "height": (height, (int, float)),
        "pos_x": (pos_x, (int, float)),
        "pos_y": (pos_y, (int, float)),
        "confidence": (confidence, str),
    }

    for name, (value, expected_type) in expected_types.items():
        if not isinstance(value, expected_type):
            raise TypeError(
                f"Argument '{name}' expected type {expected_type}, "
                f"got {type(value).__name__} instead (value: {value!r})"
            )

    if pos_z is not None and not isinstance(pos_z, (int, float)):
        raise TypeError(
            f"Argument 'pos_z' expected type float or None, "
            f"got {type(pos_z).__name__} instead (value: {pos_z!r})"
        )

    if not cursor:
        raise Exception('Cursor not found')

    select_query_id = """
    SELECT COALESCE(MAX(id), -1) + 1 FROM logs;
    """
    cursor.execute(select_query_id)
    new_id = cursor.fetchall()[0][0]

    select_query_item = """
    SELECT COUNT(*) FROM items
    WHERE name = %s;
    """
    cursor.execute(select_query_item, (item, ))

    if cursor.fetchall()[0][0] == 0:
        insert_item(cursor, item)

    select_query_item_id = """
    SELECT id FROM items
    WHERE name = %s;
    """
    cursor.execute(select_query_item_id, (item, ))
    item_id = cursor.fetchall()[0][0]

    insert_query = """
    INSERT INTO logs
    (id, item_id, time, color, center_pixel_left_x, center_pixel_left_y,
     center_pixel_right_x, center_pixel_right_y, width, height,
     position_x, position_y, position_z, confidence, notes)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
    """
    cursor.execute(
        insert_query,
        (
            new_id,
            item_id,
            time,
            color,
            center_pixel_left_x,
            center_pixel_left_y,
            center_pixel_right_x,
            center_pixel_right_y,
            width,
            height,
            pos_x,
            pos_y,
            pos_z,
            confidence,
            notes,
        ),
    )


def wipe_logs(cursor) -> None:
    if not cursor:
        raise Exception('Cursor not found')
        return
    
    query = """
    DELETE FROM logs;
    """
    cursor.execute(query)