from pathlib import Path
import os
import shutil
import sys
from tempfile import mkdtemp


ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'services/api'), str(ROOT / 'packages')]

RUNTIME_PATH = Path(mkdtemp(prefix='zhangqing-ai-playwright-'))
DATABASE_PATH = RUNTIME_PATH / 'app.sqlite3'
STORAGE_PATH = RUNTIME_PATH / 'storage'
os.environ['DATABASE_URL'] = f'sqlite:///{DATABASE_PATH}'
os.environ['DEMO_MODE'] = 'false'
os.environ['STORAGE_DIR'] = str(STORAGE_PATH)

from app.db import models  # noqa: E402,F401
from app.db.base import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.security import hash_password  # noqa: E402
from ai.suggestions import (ConfiguredAccountingSuggestionProvider, FixtureRuleSuggestionProvider,
    SuggestedLine, SuggestionProviderError, SuggestionTrace, VoucherSuggestion)  # noqa: E402
from app.routers.workflow import get_accounting_suggestion_provider  # noqa: E402


class PlaywrightOnlySuggestionProvider:
    """Deterministic UI fixture; never represents a real model call."""
    def suggest(self, *, net, tax, gross, evidence_refs, policy, context=None):
        if context.business_type == 'PURCHASE':
            candidate = FixtureRuleSuggestionProvider().suggest(net=net, tax=tax, gross=gross,
                evidence_refs=evidence_refs, policy={'fixture_only': True, 'business_use': 'resale_inventory'})
        else:
            candidate = ConfiguredAccountingSuggestionProvider().suggest(net=net, tax=tax, gross=gross,
                evidence_refs=evidence_refs, policy={**context.policy_summary, 'policy_ref': context.policy_summary['policy_ref']})
        return VoucherSuggestion(provider='PLAYWRIGHT_TEST_STUB', policy_ref=candidate.policy_ref,
            warnings=('仅 Playwright 测试替身，不是真实 DeepSeek 输出',),
            policy_explanation='Playwright 自动化测试替身',
            trace=SuggestionTrace(provider='PLAYWRIGHT_TEST_STUB', model_version='test-stub',
                prompt_version='playwright-fixture-v1', input_digest='a' * 64,
                output_digest='b' * 64, call_status='TEST_STUB', validation_status='PASSED'),
            lines=tuple(SuggestedLine(line.account_code, line.account_name, line.debit, line.credit,
                line.description, line.evidence_refs, 'Playwright 自动化测试理由') for line in candidate.lines))


class PlaywrightFailingSuggestionProvider:
    """Deterministic failure fixtures for UI blocking behavior."""
    def suggest(self, **_kwargs):
        if os.environ.get('PLAYWRIGHT_AI_FAILURE_E2E') == 'invalid_structure':
            raise SuggestionProviderError("DeepSeek 返回凭证候选结构无效", trace=SuggestionTrace(
                provider="DEEPSEEK", model_version="test-model", prompt_version="voucher-suggestion-v1",
                input_digest="a" * 64, output_digest="b" * 64,
                call_status="SUCCEEDED", validation_status="FAILED"))
        raise SuggestionProviderError("DeepSeek 请求超时", trace=SuggestionTrace(
            provider="DEEPSEEK", model_version="test-model", prompt_version="voucher-suggestion-v1",
            input_digest="a" * 64, output_digest=None, call_status="TIMEOUT", validation_status="FAILED"))


if os.environ.get('PLAYWRIGHT_AI_FAILURE_E2E') in {'timeout', 'invalid_structure'}:
    app.dependency_overrides[get_accounting_suggestion_provider] = PlaywrightFailingSuggestionProvider
elif os.environ.get('REAL_DEEPSEEK_E2E') != '1':
    app.dependency_overrides[get_accounting_suggestion_provider] = PlaywrightOnlySuggestionProvider


TENANT_ID = 'playwright-tenant'
ACCOUNT_SET_ID = 'playwright-account-set'
PASSWORD = 'Playwright-test-password-2026!'


def seed() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        tenant = models.Tenant(id=TENANT_ID, name='Playwright 测试企业', is_demo=False)
        db.add(tenant)
        db.flush()
        account_set = models.AccountSet(
            id=ACCOUNT_SET_ID, tenant_id=TENANT_ID, name='Playwright 一般纳税人测试账套', currency='CNY'
        )
        db.add(account_set)
        db.flush()
        db.add(models.AccountingPeriod(
            account_set_id=ACCOUNT_SET_ID, period='2024-12', status='OPEN', updated_by='playwright-seed'
        ))
        db.add_all([
            models.ChartAccount(account_set_id=ACCOUNT_SET_ID, code='1002', name='银行存款'),
            models.ChartAccount(account_set_id=ACCOUNT_SET_ID, code='1405', name='库存商品'),
            models.ChartAccount(account_set_id=ACCOUNT_SET_ID, code='222101', name='进项税额'),
            models.ChartAccount(account_set_id=ACCOUNT_SET_ID, code='6601', name='管理费用'),
            models.ChartAccount(account_set_id=ACCOUNT_SET_ID, code='1601', name='固定资产'),
        ])
        users = [
            models.AppUser(
                email='accountant@playwright.example.test', display_name='测试会计',
                password_hash=hash_password(PASSWORD),
            ),
            models.AppUser(
                email='approver@playwright.example.test', display_name='测试审批人',
                password_hash=hash_password(PASSWORD),
            ),
            models.AppUser(
                email='admin@playwright.example.test', display_name='测试管理员',
                password_hash=hash_password(PASSWORD),
            ),
        ]
        db.add_all(users)
        db.flush()
        db.add_all([
            models.TenantMembership(
                user_id=users[0].id, tenant_id=TENANT_ID,
                account_set_id=ACCOUNT_SET_ID, role='ACCOUNTANT',
            ),
            models.TenantMembership(
                user_id=users[1].id, tenant_id=TENANT_ID,
                account_set_id=ACCOUNT_SET_ID, role='APPROVER',
            ),
            models.TenantMembership(
                user_id=users[2].id, tenant_id=TENANT_ID,
                account_set_id=ACCOUNT_SET_ID, role='TENANT_ADMIN',
            ),
        ])


if __name__ == '__main__':
    import uvicorn

    try:
        seed()
        uvicorn.run(app, host='127.0.0.1', port=int(os.environ.get('E2E_API_PORT', '0')), log_level='warning')
    finally:
        engine.dispose()
        shutil.rmtree(RUNTIME_PATH, ignore_errors=True)
