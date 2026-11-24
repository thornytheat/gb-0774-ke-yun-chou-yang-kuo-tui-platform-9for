from app.modules.ledger.identity import sha


def station_tag(run_id):
    """站档在合档内的短标签。

    取站档编号摘要前 8 位，只取决于站档本身，与提交先后、甲乙位置无关，因此
    同一对站档无论谁先提交，合档编号与核算结果都一致。
    """
    return f's{sha(run_id)[:8]}'
