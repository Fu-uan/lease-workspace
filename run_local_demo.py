"""Start isolated demo with synthetic accounts. Never imports WorkBuddy data."""
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parent
os.environ['ZL_STORAGE'] = 'local'
os.environ.setdefault('ZL_DATA_DIR', str(ROOT / '.demo-data'))
os.environ['ZL_HOST'] = '127.0.0.1'
os.environ.setdefault('PORT', '18700')
folder = Path(os.environ['ZL_DATA_DIR'])
folder.mkdir(parents=True, exist_ok=True)
secret_file = folder / 'session.key'
if not secret_file.exists():
    secret_file.write_text(secrets.token_hex(32), encoding='utf-8')
os.environ['ZL_SESSION_KEY'] = secret_file.read_text(encoding='utf-8').strip()
import backend as B

for account, name, role in [('demo-keeper','演示维护人',B.ROLE_KEEPER),
                             ('demo-reviewer','演示审批人',B.ROLE_APPROVER),
                             ('demo-payment','演示付款审批人','付款审批人')]:
    if not B.find_user(account):
        B.lc.add(B.TBL['users_roles'], [{'登录账号':{'text':account},'姓名':{'text':name},
            '角色':{'select':role},'启用':{'checkbox':True},
            '密码哈希':{'text':B.hash_pwd('Demo-lease-2026!')}}])

if __name__ == '__main__':
    B.main()
