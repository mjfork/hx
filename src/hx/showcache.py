"""trk show results kept between passes of one watcher."""
REFRESH_S = 600


class ShowCache:
    def __init__(self):
        self.items: dict[str, tuple[dict, float]] = {}

    def clear(self) -> None:
        self.items.clear()

    def forget(self, ids) -> None:
        for i in ids:
            self.items.pop(i, None)

    def get(self, trk, item_id: str, now: float) -> dict:
        """The item as `trk show` gave it. Read again when missing or REFRESH_S old: a change the
        feed names is dropped by forget(), so the age limit only covers a change it never named."""
        hit = self.items.get(item_id)
        if hit is None or now - hit[1] >= REFRESH_S:
            hit = (trk.show(item_id)["item"], now)
            self.items[item_id] = hit
        return hit[0]
