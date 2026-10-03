"""Optional copied categories; never a shared mutable category tree."""

from types import MappingProxyType

from coinpup_api.ledger.schemas import TemplateCategory, TemplateResponse


def _category(key, name, name_en, kind="expense"):
    return TemplateCategory(key=key, name=name, name_en=name_en, kind=kind)


TEMPLATES = MappingProxyType(
    {
        "personal_default": TemplateResponse(
            key="personal_default",
            name="个人常用分类",
            name_en="Personal categories",
            categories=(
                _category("food", "餐饮", "Food"),
                _category("transport", "交通", "Transport"),
                _category("housing", "居住", "Housing"),
                _category("shopping", "购物", "Shopping"),
                _category("health", "健康", "Health"),
                _category("salary", "工资", "Salary", "income"),
                _category("other_income", "其他收入", "Other income", "income"),
            ),
        ),
        "business_default": TemplateResponse(
            key="business_default",
            name="公司常用分类",
            name_en="Business categories",
            categories=(
                _category("software", "软件服务", "Software"),
                _category("office", "办公", "Office"),
                _category("travel", "差旅", "Travel"),
                _category("professional", "专业服务", "Professional services"),
                _category("bank_fees", "账户手续费", "Account fees"),
                _category("sales", "销售收入", "Sales", "income"),
                _category("other_income", "其他收入", "Other income", "income"),
            ),
        ),
    }
)


def get_templates() -> list[TemplateResponse]:
    return list(TEMPLATES.values())
