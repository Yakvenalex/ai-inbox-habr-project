### Проверка безопасного Markdown

Обычный **жирный текст**, *курсив*, `inline_code` и [безопасная ссылка](https://example.test/).

- Первый пункт
- Второй пункт

```python
print("Код остаётся текстом")
```

<script>document.body.dataset.markdownExecuted = 'yes'</script>
<img src="https://example.invalid/tracker" onerror="document.body.dataset.markdownExecuted='yes'">
<iframe src="https://example.invalid/frame"></iframe>
[Небезопасный протокол](javascript:alert(1))
![Картинка не загружается](https://example.invalid/tracker.png)
