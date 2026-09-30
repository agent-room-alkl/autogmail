from dataclasses import dataclass, field


@dataclass
class Mail:
    id: str
    thread_id: str
    from_name: str
    from_email: str
    reply_email: str
    subject: str
    date: str
    body: str
    headers: dict[str, str] = field(default_factory=dict)
    label_ids: list[str] = field(default_factory=list)
    snippet: str = ""
