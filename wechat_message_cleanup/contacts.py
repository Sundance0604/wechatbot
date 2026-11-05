"""Local contact display-name mapping; never grants an operator role."""
import os


def load_contact_names(db):
    for rel,path,_ in db._db_files:
        if os.path.basename(path)!='contact.db':continue
        conn=db._open(rel)
        try:
            return {row[0]:row[2] or row[1] or row[0]
                    for row in conn.execute('SELECT username,nick_name,remark FROM contact') if row[0]}
        finally:conn.close()
    return {}
