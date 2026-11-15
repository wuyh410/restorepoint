"""Convert terminal output to plain task logs without authentication feedback."""
import re

ESCAPE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]')
SUDO_PROMPT = re.compile(
    r'^(?P<time>\d{2}:\d{2}:\d{2}\s+)?\s*\[sudo[^\]\n]*\]\s*'
    r'(?:(?:password(?:\s+for\s+[^:\n]+)?|密码)\s*[:：])?\s*[*• \t]*', re.I)


def clean_log_text(text):
    text = str(text).replace('__RESTOREPOINT_SUDO__', '')
    lines, cells, cursor, index = [], [], 0, 0
    while index < len(text):
        escape = ESCAPE.match(text, index)
        if escape:
            code = escape.group()
            if code.startswith('\x1b[') and code.endswith('K'):
                mode = code[2:-1] or '0'
                if mode == '2':
                    cells, cursor = [], 0
                elif mode == '1':
                    cells[:cursor + 1] = [' '] * min(cursor + 1, len(cells))
                else:
                    del cells[cursor:]
            index = escape.end()
            continue
        char = text[index]
        index += 1
        if char == '\n':
            lines.append(''.join(cells).rstrip())
            cells, cursor = [], 0
        elif char == '\r':
            cursor = 0
        elif char in ('\b', '\u2408'):
            cursor = max(0, cursor - 1)
        elif char == '\t':
            count = 8 - cursor % 8
            cells[cursor:cursor + count] = [' '] * count
            cursor += count
        elif char.isprintable():
            if cursor < len(cells):
                cells[cursor] = char
            else:
                cells.append(char)
            cursor += 1
    lines.append(''.join(cells).rstrip())
    cleaned = []
    for line in lines:
        prompt = SUDO_PROMPT.match(line)
        if prompt:
            rest = line[prompt.end():]
            line = (prompt.group('time') or '') + rest if rest.strip() else ''
        if line.strip() and not re.fullmatch(r'[\s*•]+', line):
            cleaned.append(line)
    return '\n'.join(cleaned)


class LogLines:
    def __init__(self):
        self.pending = ''

    def feed(self, text):
        self.pending += text
        parts = self.pending.split('\n')
        self.pending = parts.pop()
        return [line for part in parts if (line := clean_log_text(part))]

    def finish(self):
        line = clean_log_text(self.pending)
        self.pending = ''
        return [line] if line else []
