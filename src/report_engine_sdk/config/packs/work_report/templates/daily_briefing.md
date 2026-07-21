# 每日简报

**用户**: {{ user_name }}
**日期**: {{ date_str }}

## 今日总结

{{ summary_text }}

## 待办事项

{% for item in action_items %}
- {{ item }}
{% endfor %}

---
*本简报由智能体自动生成*
