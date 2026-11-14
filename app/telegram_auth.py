"""Telegram Mini App authentication. Never trust initDataUnsafe or a user ID alone."""
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

MAX_AGE = 300


def allowed_ids():
    # Explicit allowlist, fail closed; chat IDs are not a substitute for user IDs.
    return {int(x.strip()) for x in os.getenv('ALLOWED_TELEGRAM_USER_IDS', '').split(',')
            if x.strip().isascii() and x.strip().isdigit() and int(x.strip()) > 0}


class TelegramAuthError(ValueError):
    pass


def validate_init_data(raw, bot_token, now=None):
    if not bot_token or not raw or len(raw) > 16384:
        raise TelegramAuthError('Не удалось проверить данные Telegram')
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=64, errors='strict')
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError('duplicate field')
        received = fields.pop('hash')
        if len(received) != 64 or any(ch not in '0123456789abcdef' for ch in received):
            raise ValueError('invalid hash')
        check = '\n'.join(f'{k}={v}' for k, v in sorted(fields.items()))
        key = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received, expected):
            raise ValueError('invalid signature')
        auth_date = int(fields['auth_date'])
        now = time.time() if now is None else now
        if not now - MAX_AGE <= auth_date <= now + 30:
            raise TelegramAuthError('Данные Telegram устарели. Закройте и снова откройте Mini App.')
        user = json.loads(fields['user'])
        uid = user['id']
        if type(uid) is not int or uid <= 0:
            raise ValueError('invalid user')
    except TelegramAuthError:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise TelegramAuthError('Не удалось проверить данные Telegram') from None
    return uid, fields.get('start_param', '')
