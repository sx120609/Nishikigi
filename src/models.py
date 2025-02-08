from dataclasses import dataclass, field
from peewee import (
    Model,
    SqliteDatabase,
    IntegerField,
    TextField,
    AutoField,
    TimestampField,
    BooleanField,
)

db = SqliteDatabase("data.db")


class Article(Model):
    id = AutoField()

    sender_id = IntegerField(null=False)
    sender_name = TextField(null=True)
    """ None: 未完成投稿; 0: 已完成投稿; "": 通过审核 """
    tid = TextField(null=True)
    time = TimestampField()

    single = BooleanField()

    class Meta:
        database = db


Article.create_table(safe=True)


@dataclass(slots=True)
class Session:
    id: int
    anonymous: bool
    contents: list = field(default_factory=list)
