# 学习画像报告

**学生**: {{ student_name }}
**年级**: {{ grade }}
**报告日期**: {{ report_date }}

## 学业概况

| 指标 | 数值 |
|------|------|
| GPA | {{ gpa }} |
| 班级排名 | {{ class_rank }}/{{ class_size }} |
| 学习积分 | {{ learning_points }} |
| 学习等级 | **{{ learning_level }}** |

## 学习行为分析

- 在线学习时长：{{ online_hours }} 小时
- 作业完成率：{{ homework_completion_rate }}%
- 互动参与度：{{ participation_score }}%

## 优势与待提升

**优势领域**：
{% for strength in strengths %}
- {{ strength }}
{% endfor %}

**待提升领域**：
{% for weakness in weaknesses %}
- {{ weakness }}
{% endfor %}

## 个性化建议

> 基于当前学习画像，建议重点加强 {{ focus_area }} 的学习，保持 {{ maintain_area }} 的持续进步。
