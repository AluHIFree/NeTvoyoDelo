(function () {
    const form = document.getElementById('aiReportForm');
    if (!form) return;

    const btn = document.getElementById('aiGenerateBtn');
    const statusEl = document.getElementById('aiStatus');
    const resultBox = document.getElementById('aiResult');
    const reportText = document.getElementById('aiReportText');
    const resultMeta = document.getElementById('aiResultMeta');
    const aiConfigured = document.body.dataset.aiConfigured === '1';

    form.addEventListener('submit', async function (e) {
        e.preventDefault();
        statusEl.className = 'ai-status';
        statusEl.textContent = 'Анализирую закрытые письма и формирую отчёт…';
        btn.disabled = true;
        resultBox.classList.remove('show');

        const fd = new FormData(form);
        try {
            const res = await fetch('/profile/ai-report', {
                method: 'POST',
                body: fd,
                credentials: 'same-origin',
            });
            const data = await res.json();
            if (!data.ok) {
                statusEl.className = 'ai-status error';
                statusEl.textContent = data.error || 'Не удалось сформировать отчёт';
                return;
            }
            reportText.textContent = data.report || '';
            resultMeta.textContent =
                'Период: ' + (data.period_title || '—') + ' · писем: ' + (data.letters_count || 0);
            resultBox.classList.add('show');
            statusEl.className = 'ai-status ok';
            statusEl.textContent = 'Готово';
        } catch (err) {
            statusEl.className = 'ai-status error';
            statusEl.textContent = 'Ошибка сети или сервера';
        } finally {
            btn.disabled = !aiConfigured;
        }
    });

    const copyBtn = document.getElementById('aiCopyBtn');
    if (copyBtn) {
        copyBtn.addEventListener('click', async function () {
            const text = reportText.textContent || '';
            try {
                await navigator.clipboard.writeText(text);
                statusEl.className = 'ai-status ok';
                statusEl.textContent = 'Скопировано в буфер';
            } catch (_) {
                statusEl.className = 'ai-status error';
                statusEl.textContent = 'Не удалось скопировать';
            }
        });
    }

    const downloadBtn = document.getElementById('aiDownloadBtn');
    if (downloadBtn) {
        downloadBtn.addEventListener('click', function () {
            const text = reportText.textContent || '';
            const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            const fromEl = document.getElementById('ai_date_from');
            const from = (fromEl && fromEl.value) || 'period';
            a.href = url;
            a.download = 'otchet_' + from + '.txt';
            a.click();
            URL.revokeObjectURL(url);
        });
    }
})();
