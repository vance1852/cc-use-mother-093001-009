"""纹样授权计费与结算服务。

在 creative_program_foundation 提供的 SQLite 事务、幂等回执、审计链和时钟
等稳定边界上，实现授权作品、费率版本、使用事实导入、不可覆盖计费分录、
阶梯合并判断、关账冻结、争议托管、追补冲销、待追偿、付款与权利人分账。
"""

from .service import LicensingService

__all__ = ["LicensingService"]
