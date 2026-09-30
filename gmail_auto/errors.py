class ConfigError(Exception):
    """配置、授权或本机记录有问题。消息给界面和终端看，用中文。"""


class ProfileError(ConfigError):
    """个人资料不符合要求。"""


class ReplyError(ConfigError):
    """这封信这一轮没有生成可发送的回复。"""
