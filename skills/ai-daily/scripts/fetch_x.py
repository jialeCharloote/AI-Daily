#!/usr/bin/env python3
"""Fetch today's X.com posts from AI accounts via twscrape.
Credentials are read from ~/.claude/private/x-creds.json
(%USERPROFILE%\\.claude\\private\\x-creds.json on Windows)
or from the X_AUTH_TOKEN / X_CT0 environment variables.
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

# On Windows, curl-cffi ignores system proxy settings (Clash, V2Ray, etc.) stored
# in the registry. Read the registry and populate env vars before twscrape imports.
if sys.platform == 'win32' and not os.environ.get('HTTPS_PROXY'):
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Software\Microsoft\Windows\CurrentVersion\Internet Settings')
        enabled, _ = winreg.QueryValueEx(key, 'ProxyEnable')
        if enabled:
            proxy, _ = winreg.QueryValueEx(key, 'ProxyServer')
            if proxy and '://' not in proxy:
                proxy = 'http://' + proxy
            os.environ['HTTP_PROXY'] = proxy
            os.environ['HTTPS_PROXY'] = proxy
            os.environ['ALL_PROXY'] = proxy
        winreg.CloseKey(key)
    except Exception:
        pass

# The curl backend needs curl-cffi, which only ships with twscrape[curl] — a plain
# `pip install twscrape` does not pull it in. Without it twscrape raises ImportError
# on the first account, marks that account locked, and every later call then waits
# forever for an account that can never unlock: no output, no error, no exit.
#
# Bail out here instead. Falling back to the default httpx backend is not a fix —
# X.com does not answer its requests and they hang with no timeout, which turns a
# missing dependency into the same silent deadlock.
try:
    import curl_cffi  # noqa: F401
except ImportError:
    print('[FATAL] curl-cffi is required but not installed. Without it this script '
          'hangs forever instead of failing. Install with:\n'
          '  pip install --upgrade "twscrape[curl] @ '
          'git+https://github.com/vladkens/twscrape.git"', file=sys.stderr)
    print('[]')
    sys.exit(1)

os.environ['TWS_HTTP_BACKEND'] = 'curl'

from twscrape import API, gather
from twscrape.logger import set_log_level
set_log_level('ERROR')

CREDS_FILE = os.path.join(os.path.expanduser('~'), '.claude', 'private', 'x-creds.json')

X_ACCOUNTS = [
    "GoogleLabs", "nickstpierre", "mattturck", "karpathy",
    "garrytan", "levie", "HamelHusain", "alexalbert__",
    "rauchg", "amasad", "george__mack", "mckaywrigley",
    "lennysan", "gregisenberg", "swyx", "kevinweil",
    "joshwoodward", "peteryang",
    # Chinese labs. Handles verified against the live API — several plausible
    # guesses are impostors or unrelated accounts: @ChatGLM (10 followers),
    # @moonshotai (an unrelated "Chancellor Moonshot"), @deepseek (474), and
    # @InternLM (a medical account) are NOT these labs. Do not "correct" these.
    "deepseek_ai",      # DeepSeek
    "Alibaba_Qwen",     # Qwen
    "Kimi_Moonshot",    # Moonshot AI
    "Zai_org",          # Z.ai / 智谱 (formerly ChatGLM)
    "MiniMax_AI",       # MiniMax
    # Research/papers tier (friend-recommended, verified 2026-08-17):
    "JeffDean",         # Jeff Dean, Google chief scientist
    "hardmaru",         # David Ha, Sakana AI
    "fly51fly",         # AI paper curator
    "sama",             # Sam Altman
    "gdb",              # Greg Brockman
    "emilychangtv",     # Emily Chang, Bloomberg
]

# Markets section — kept apart from the AI accounts on purpose: their posts get
# section="markets", skip the AI keyword filter (finance content would never
# match it), and render in their own section of the brief. financein90s and
# kuntupark were also recommended but do not exist on X (TikTok-only).
MARKET_ACCOUNTS = [
    "cantonmeow",       # technical analysis
    "jiahanjimliu",     # Jim Liu, IREN / AI-infra equities
    "thedealsguy_",     # consumer retail arbitrage
    "liamdaltonjr",
]

MAX_POSTS_PER_MARKET_ACCOUNT = 2

# Accounts whose every post is on-topic by definition. Official lab accounts get a
# pass on the keyword filter: an announcement like "DeepSeek-V4 is live" contains
# none of the keywords below and would otherwise be dropped, and these labs post in
# Chinese as often as English.
# Most posts kept from any one account, applied after the engagement sort.
MAX_POSTS_PER_ACCOUNT = 3

ALWAYS_RELEVANT = {
    "deepseek_ai", "Alibaba_Qwen", "Kimi_Moonshot", "Zai_org", "MiniMax_AI",
    "GoogleLabs",
}

# Post must contain at least one of these keywords (case-insensitive) to be included.
#
# Short acronyms are matched as whole words only. Plain substring matching let 'ai'
# hit 'air' and 'Aisha', which pulled unrelated news and political posts into the
# results. A trailing 's' is allowed so 'llms' and 'apis' still match.
AI_ACRONYMS = ['ai', 'ml', 'llm', 'gpt', 'rag', 'api', 'saas']

# Matched from a word boundary but allowed to run on, so 'model' catches 'models',
# 'deploy' catches 'deployment', and 'fine-tun' catches 'fine-tuning'.
AI_STEMS = [
    'claude', 'gemini', 'openai', 'anthropic', 'intelligence',
    'model', 'agent', 'prompt', 'token', 'inference', 'training', 'neural',
    'embedding', 'vector', 'fine-tun', 'transformer', 'diffusion',
    'multimodal', 'frontier', 'open weight', 'open-weight',
    'chatgpt', 'copilot', 'cursor', 'replit', 'automation',
    'machine learning', 'deep learning', 'foundation model', 'language model',
    'benchmark', 'eval', 'alignment', 'vibe cod', 'coding assistant',
    'startup', 'founder', 'product', 'software', 'developer',
    'open source', 'dataset', 'research', 'paper', 'deploy',
]

# Chinese terms are matched as plain substrings. CJK has no word boundaries for \b
# to find, and these are multi-character terms, so the 'ai'-inside-'air' class of
# false positive does not arise here.
AI_TERMS_CN = [
    '模型', '大模型', '智能体', '推理', '训练', '微调', '开源', '多模态',
    '参数', '算力', '提示词', '语料', '对齐', '基准测试', '生成式',
    '人工智能', '深度学习', '机器学习', '发布', '上线', '权重',
]

_ACRONYM_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(k) for k in AI_ACRONYMS) + r')s?\b', re.IGNORECASE)
_STEM_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(k) for k in AI_STEMS) + r')', re.IGNORECASE)

def is_ai_relevant(text: str, username: str = '') -> bool:
    if username in ALWAYS_RELEVANT:
        return True
    if any(term in text for term in AI_TERMS_CN):
        return True
    return bool(_ACRONYM_RE.search(text) or _STEM_RE.search(text))

def load_creds():
    auth_token = os.environ.get('X_AUTH_TOKEN')
    ct0 = os.environ.get('X_CT0')
    if auth_token and ct0:
        return auth_token, ct0
    with open(CREDS_FILE, 'r') as f:
        creds = json.load(f)
    return creds['auth_token'], creds['ct0']

async def main():
    try:
        auth_token, ct0 = load_creds()
    except Exception as e:
        print(json.dumps({"error": f"Could not load credentials: {e}"}))
        sys.exit(1)

    beijing_tz = timezone(timedelta(hours=8))
    today = datetime.now(beijing_tz).date()
    yesterday = today - timedelta(days=1)

    api = API()
    await api.pool.add_account_cookies('morning_tea_account', f'auth_token={auth_token}; ct0={ct0}')

    results = []
    all_accounts = ([(u, 'ai') for u in X_ACCOUNTS]
                    + [(u, 'markets') for u in MARKET_ACCOUNTS])
    for username, section in all_accounts:
        try:
            user = await api.user_by_login(username)
            if not user:
                continue
            tweets = await gather(api.user_tweets(user.id, limit=10))
            for tweet in tweets:
                tweet_date = tweet.date.astimezone(beijing_tz).date()
                if tweet_date in [today, yesterday]:
                    # Emit every in-window post and record why it was dropped,
                    # rather than dropping it here. Silently discarded posts made
                    # the keyword filter's false-negative rate unmeasurable: the
                    # posts you most need to audit were the ones never written.
                    # Market posts skip the AI keyword filter — finance content
                    # would never match it and lives in its own brief section.
                    if tweet.rawContent.startswith('RT @'):
                        status = 'dropped_rt'
                    elif section == 'ai' and not is_ai_relevant(tweet.rawContent, username):
                        status = 'dropped_keyword'
                    else:
                        status = 'kept'
                    results.append({
                        "username": username,
                        "displayname": user.displayname,
                        "content": tweet.rawContent,
                        "date": tweet.date.astimezone(beijing_tz).strftime('%Y-%m-%d %H:%M'),
                        "url": f"https://x.com/{username}/status/{tweet.id}",
                        "likes": tweet.likeCount,
                        "retweets": tweet.retweetCount,
                        "status": status,
                        "section": section
                    })
        except Exception as e:
            # Report and move on. Swallowing these silently made a missing
            # dependency look like a hang with no diagnostic output at all.
            print(f'[FAIL] {username}: {type(e).__name__}: {e}', file=sys.stderr)
            continue

    in_window = len(results)
    results.sort(key=lambda x: x.get('likes', 0) + x.get('retweets', 0) * 3, reverse=True)

    # Cap per account so no single one dominates the digest. Lab accounts in
    # particular amplify one launch across many posts — a single Qwen release
    # filled 11 of 15 slots in one window, five of them about the same model.
    # Sorted by engagement first, so each account keeps its strongest posts.
    # Over-cap posts are marked rather than removed, same as the filters above.
    # Market accounts get a tighter cap: high-engagement trading posts would
    # otherwise swamp their small section.
    per_account = {}
    for post in results:
        if post['status'] != 'kept':
            continue
        user = post['username']
        cap = (MAX_POSTS_PER_MARKET_ACCOUNT if post.get('section') == 'markets'
               else MAX_POSTS_PER_ACCOUNT)
        per_account[user] = per_account.get(user, 0) + 1
        if per_account[user] > cap:
            post['status'] = 'dropped_cap'

    # Consumers take the leading run of kept posts, so order by status first,
    # section second (ai before markets), engagement third. Everything after
    # the kept block is archive-only.
    kept = [p for p in results if p['status'] == 'kept']
    kept.sort(key=lambda p: p.get('section', 'ai') != 'ai')
    dropped = [p for p in results if p['status'] != 'kept']
    results = kept + dropped

    tally = {}
    for post in dropped:
        tally[post['status']] = tally.get(post['status'], 0) + 1
    detail = ', '.join(f'{k}={v}' for k, v in sorted(tally.items()))
    n_mkt = sum(1 for p in kept if p.get('section') == 'markets')
    print(f'[OK] {len(kept)} kept ({len(kept) - n_mkt} ai + {n_mkt} markets) of '
          f'{in_window} in-window posts from '
          f'{len(X_ACCOUNTS) + len(MARKET_ACCOUNTS)} accounts'
          + (f' ({detail})' if detail else ''),
          file=sys.stderr)
    sys.stdout.buffer.write(json.dumps(results, ensure_ascii=False).encode('utf-8'))
    sys.stdout.buffer.write(b'\n')

asyncio.run(main())
