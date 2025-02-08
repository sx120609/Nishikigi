import base64


def read_image(path: str) -> bytes:
    with open(path, mode="br") as f:
        return base64.b64encode(f.read())

def to_list(l):
    return list(map(lambda a: a.id, l))