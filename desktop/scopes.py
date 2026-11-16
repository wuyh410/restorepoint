"""Cross-platform descriptions of the ranges present in a backup."""
AREAS = ('system', 'config', 'software', 'home')
DEFAULT = ('system', 'home')
NAMES = {'system': '系统', 'config': '系统配置', 'software': '软件', 'home': '用户目录'}


def normalize(values=None):
    values = DEFAULT if values is None else values
    if not isinstance(values, (list, tuple)) or not values or any(not isinstance(value, str) or value not in AREAS for value in values) or len(set(values)) != len(values):
        raise ValueError('请选择有效且不重复的备份范围。')
    return tuple(area for area in AREAS if area in values and not (area in ('config', 'software') and 'system' in values))


def contained(data):
    if data.get('schema', 1) == 1:
        return DEFAULT
    if data.get('schema') != 2 or data.get('areas') is None:
        raise ValueError('不支持的备份范围清单。')
    areas = normalize(data['areas'])
    if set(areas) != set(data['areas']):
        raise ValueError('备份范围清单无效。')
    return areas


def title(areas):
    values = normalize(areas)
    return '完整备份' if set(values) == set(DEFAULT) else ' + '.join(NAMES[area] for area in values)


def restorable(data):
    areas = contained(data)
    result = []
    if 'home' in areas:
        result.append(('用户目录 / 文件', 'home'))
    if 'system' in areas:
        result.append(('系统与已安装软件 · 整体回滚', 'system'))
    if 'config' in areas or 'system' in areas:
        result.append(('系统配置 /etc', 'config'))
    if 'software' in areas or 'system' in areas and 'software' in data.get('excludes', {}):
        result.append(('软件文件 / 配置与数据 · 复制恢复', 'software'))
        files = set(data.get('files', {})) | set(data.get('metadata_sha256', {}))
        if {'packages.tsv', 'os-release.json'} <= files:
            result.append(('按软件清单重新安装 · apt', 'packages'))
    return result
