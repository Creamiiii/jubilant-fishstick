from func import *
import mysql.connector
import ref


def main(cursor):
    wipe_logs(cursor)
    wipe_items(cursor)


if __name__ == '__main__':
    try:
        conn = mysql.connector.connect(
            host=ref.host,
            user=ref.user,
            password=ref.password,
            database=ref.database
        )

        # Connection Succeeded
        if conn.is_connected():
            cursor = conn.cursor()
            main(cursor)

    except mysql.connector.Error as e:
        print(f'Error: {e}')

    finally:
        conn.commit()
        conn.close()
        print('Exited with code 0')
