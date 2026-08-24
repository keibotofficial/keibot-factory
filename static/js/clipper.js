document.addEventListener('DOMContentLoaded', () => {
    // --- MANAJEMEN MULTI-PROVIDER API KEY ---
    const providerLinks = {
        'huggingface': { url: 'https://huggingface.co/settings/tokens', text: 'Ambil Token HF di sini' },
        'groq': { url: 'https://console.groq.com/keys', text: 'Ambil API Key Groq di sini' },
        'gemini': { url: 'https://aistudio.google.com/app/apikey', text: 'Ambil API Key Gemini di sini' },
        'deepseek': { url: 'https://platform.deepseek.com/api_keys', text: 'Ambil API Key DeepSeek di sini' }
    };

    let apiKeys = JSON.parse(localStorage.getItem('keibot_api_keys')) || {
        huggingface: JSON.parse(localStorage.getItem('keibot_hf_tokens') || '[]'),
        groq: [], gemini: [], deepseek: []
    };

    const providerSelect = document.getElementById('apiProviderSelect');
    const apiKeyLink = document.getElementById('apiKeyLink');
    const tokenInput = document.getElementById('apiTokenInput');

    if (providerSelect) {
        providerSelect.addEventListener('change', (e) => {
            const provider = e.target.value;
            if (apiKeyLink) {
                apiKeyLink.href = providerLinks[provider].url;
                apiKeyLink.innerText = providerLinks[provider].text;
            }
            renderTokens();
        });
    }

    function renderTokens() {
        if (!providerSelect) return;
        const provider = providerSelect.value;
        const tokens = apiKeys[provider] || [];
        const list = document.getElementById('tokenList');
        
        if (!list) return;
        if (tokens.length === 0) {
            list.innerHTML = '<div class="text-center text-[10px] text-gray-500 py-2">Belum ada API Key tersimpan.</div>';
            return;
        }
        list.innerHTML = tokens.map((t, i) => `
            <div class="flex justify-between items-center bg-[#0a0f1c] px-3 py-2 rounded border border-gray-800 mb-1">
                <span class="text-xs text-gray-400 font-mono">...${t.slice(-8)}</span>
                <button onclick="removeToken(${i})" class="text-red-500 hover:text-red-400 text-[10px] bg-red-500/10 px-2 py-1 rounded"><i class="fa-solid fa-trash"></i></button>
            </div>
        `).join('');
    }

    window.addToken = function() {
        if (!providerSelect || !tokenInput) return;
        const provider = providerSelect.value;
        const val = tokenInput.value.trim();
        if (val) {
            if (!apiKeys[provider]) apiKeys[provider] = [];
            apiKeys[provider].push(val);
            localStorage.setItem('keibot_api_keys', JSON.stringify(apiKeys));
            tokenInput.value = '';
            renderTokens();
        }
    };

    window.removeToken = function(idx) {
        if (!providerSelect) return;
        const provider = providerSelect.value;
        apiKeys[provider].splice(idx, 1);
        localStorage.setItem('keibot_api_keys', JSON.stringify(apiKeys));
        renderTokens();
    };

    if (providerSelect) providerSelect.dispatchEvent(new Event('change'));

    // --- LOAD CHANNELS DARI FACTORY ---
    async function loadChannels() {
        try {
            const res = await fetch('/api/get_channels');
            const channels = await res.json();
            const sel = document.getElementById('channelSelect');
            const assetSel = document.getElementById('assetChannelSelect');
            if (sel) {
                channels.forEach(c => {
                    sel.innerHTML += `<option value="${c.yt_id}">${c.name}</option>`;
                    if (assetSel) assetSel.innerHTML += `<option value="${c.yt_id}">${c.name}</option>`;
                });
            }
        } catch(e) {}
    }
    loadChannels();

    // --- MANAJER CUSTOM FONT OTOMATIS ---
    const customFontSel = document.getElementById('customFont');
    const fontUpload = document.getElementById('fontUpload');
    const btnDeleteFont = document.getElementById('btnDeleteFont');
    const fontUploadStatus = document.getElementById('fontUploadStatus');

    async function loadFonts() {
        try {
            const res = await fetch('/api/clip/fonts');
            const json = await res.json();
            if (customFontSel && json.success) {
                customFontSel.innerHTML = '<option value="">-- Gunakan Font Bawaan --</option>';
                if (json.data.length > 0) {
                    json.data.forEach(f => {
                        customFontSel.innerHTML += `<option value="${f.name}" data-filename="${f.filename}">${f.name}</option>`;
                    });
                }
                customFontSel.dispatchEvent(new Event('change'));
            }
        } catch(e) {}
    }
    
    if (customFontSel && btnDeleteFont) {
        customFontSel.addEventListener('change', () => {
            btnDeleteFont.disabled = (customFontSel.value === "");
        });
    }

    if (fontUpload) {
        fontUpload.addEventListener('change', async (e) => {
            const file = e.target.files[0];
            if (!file) return;
            
            const formData = new FormData();
            formData.append('file', file);
            
            fontUploadStatus.classList.remove('hidden');
            fontUploadStatus.innerText = 'Mengunggah font...';
            
            try {
                const res = await fetch('/api/clip/fonts/upload', {
                    method: 'POST', body: formData
                });
                const data = await res.json();
                if (data.success) {
                    fontUploadStatus.innerText = 'Font berhasil diunggah!';
                    fontUploadStatus.classList.add('text-green-500');
                    await loadFonts();
                } else {
                    alert(data.error || 'Gagal mengunggah font.');
                }
            } catch(err) {
                alert('Error: ' + err.message);
            }
            
            setTimeout(() => { fontUploadStatus.classList.add('hidden'); fontUploadStatus.classList.remove('text-green-500'); fontUploadStatus.innerText='Mengunggah...'; }, 3000);
            fontUpload.value = ''; 
        });
    }
    
    if (btnDeleteFont) {
        btnDeleteFont.addEventListener('click', async () => {
            const selectedOpt = customFontSel.options[customFontSel.selectedIndex];
            const filename = selectedOpt.dataset.filename;
            if (!filename) return;
            if (!confirm(`Yakin ingin menghapus font ${filename}?`)) return;
            
            try {
                const res = await fetch(`/api/clip/fonts/${filename}`, { method: 'DELETE' });
                const data = await res.json();
                if (data.success) {
                    await loadFonts();
                } else {
                    alert(data.error || 'Gagal menghapus font.');
                }
            } catch(err) { alert('Error: ' + err.message); }
        });
    }
    loadFonts();

    // --- LISTENER UNTUK BGM (AUDIO LATAR) ---
    const assetChannelSelect = document.getElementById('assetChannelSelect');
    const bgmSelect = document.getElementById('bgmSelect');

    if (assetChannelSelect && bgmSelect) {
        assetChannelSelect.addEventListener('change', async (e) => {
            const channelId = e.target.value;
            if (!channelId) {
                bgmSelect.innerHTML = '<option value="none">-- Pilih Brankas Dulu --</option>';
                bgmSelect.disabled = true;
                return;
            }
            bgmSelect.innerHTML = '<option value="none">Mencari file mp3...</option>';
            bgmSelect.disabled = true;
            
            try {
                const res = await fetch(`/api/clip/assets/${channelId}`);
                const json = await res.json();
                
                if (json.success && json.data.length > 0) {
                    let options = '<option value="none">-- Tanpa Musik Latar --</option>';
                    json.data.forEach(audio => {
                        options += `<option value="${audio.value}">${audio.filename}</option>`;
                    });
                    bgmSelect.innerHTML = options;
                    bgmSelect.disabled = false;
                } else {
                    bgmSelect.innerHTML = '<option value="none">-- Folder Aset Kosong --</option>';
                    bgmSelect.disabled = false;
                }
            } catch (err) { bgmSelect.innerHTML = '<option value="none">-- Gagal Memuat BGM --</option>'; }
        });
    }

    const dateInput = document.getElementById('publishDate');
    if (dateInput) {
        const now = new Date();
        now.setMinutes(0, 0, 0);
        dateInput.value = now.toISOString().slice(0, 16);
    }

    // --- ALUR KERJA UTAMA ---
    const urlInput = document.getElementById('urlInput');
    const btnInfo = document.getElementById('btnInfo');
    const btnStartBatch = document.getElementById('btnStartBatch');
    const videoInfo = document.getElementById('videoInfo');
    const taskList = document.getElementById('taskList');
    const activeStreams = {};

    if (btnInfo) {
        btnInfo.addEventListener('click', async () => {
            if (!urlInput) return;
            const url = urlInput.value.trim();
            if (!url) return alert('Masukkan URL YouTube dulu!');
            
            btnInfo.innerText = 'Loading...'; 
            btnInfo.disabled = true;
            
            try {
                const res = await fetch('/api/clip/info', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url })
                });
                const data = await res.json();
                if (data.success) {
                    if (videoInfo) videoInfo.style.display = 'block';
                    const titleEl = document.getElementById('videoTitle');
                    const thumbEl = document.getElementById('thumbPreview');
                    const durEl = document.getElementById('videoDuration');
                    if (titleEl) titleEl.innerText = data.data.title;
                    if (thumbEl) thumbEl.src = data.data.thumbnail || '';
                    if (durEl) durEl.innerText = data.data.durationLabel;
                } else { alert(data.error?.message || 'Gagal mengambil info.'); }
            } catch (err) { alert('Koneksi error: ' + err.message); 
            } finally { btnInfo.innerText = 'Ambil Info'; btnInfo.disabled = false; }
        });
    }

    if (btnStartBatch) {
        btnStartBatch.addEventListener('click', async () => {
            if (!urlInput) return;
            const url = urlInput.value.trim();
            if (!url) return alert('Masukkan URL Video dulu!');

            const providerEl = document.getElementById('apiProviderSelect');
            const providerVal = providerEl ? providerEl.value : 'huggingface';
            const lyricStyleEl = document.getElementById('lyricStyle');
            const currentStyle = lyricStyleEl ? lyricStyleEl.value : 'none';
            
            if ((!apiKeys[providerVal] || apiKeys[providerVal].length === 0) && currentStyle !== 'none') {
                return alert(`Masukkan minimal 1 API Key ${providerVal.toUpperCase()} untuk fitur Auto-Lirik!`);
            }

            const payload = {
                url: url,
                batchCount: document.getElementById('batchCount')?.value || 1,
                cropMode: document.getElementById('cropMode')?.value || 'center',
                lyricStyle: currentStyle,
                customFont: document.getElementById('customFont')?.value || '', 
                useVisualizer: document.getElementById('useVisualizer')?.checked || false,
                bgmFile: document.getElementById('bgmSelect')?.value || 'none',
                audioMode: document.getElementById('audioMode')?.value || 'bgm',
                outputDest: document.getElementById('outputDest')?.value || 'local', 
                assetChannelId: document.getElementById('assetChannelSelect')?.value || '',
                targetChannelId: document.getElementById('channelSelect')?.value || '',
                publishDate: document.getElementById('publishDate')?.value || '',
                aiProvider: providerVal,
                apiKeys: apiKeys[providerVal] || []
            };

            btnStartBatch.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Memproses...';
            btnStartBatch.disabled = true;

            try {
                const res = await fetch('/api/clip/start', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();

                if (data.success) {
                    if (taskList && taskList.innerHTML.includes('Belum ada antrean')) taskList.innerHTML = '';
                    for(let i = 1; i <= parseInt(payload.batchCount); i++) {
                        const jobId = `${data.data.baseJobId}_${i}`;
                        createTaskElement(jobId, payload.url);
                        monitorTask(jobId);
                    }
                    alert(data.data.message);
                    urlInput.value = ''; 
                    if (videoInfo) videoInfo.style.display = 'none';
                } else { alert(data.error?.message || 'Gagal memulai antrean.'); }
            } catch (err) { alert('Gagal: ' + err.message); 
            } finally {
                btnStartBatch.innerHTML = '<i class="fa-solid fa-rocket"></i> Generate & Jadwalkan Clip';
                btnStartBatch.disabled = false;
            }
        });
    }

    function createTaskElement(jobId, url) {
        if (!taskList) return;
        const div = document.createElement('div');
        div.className = 'mb-6 bg-[#0a0f1c]/50 rounded-xl p-4 md:p-6 border border-cardborder relative overflow-hidden';
        div.id = `task-${jobId}`;
        
        div.innerHTML = `
            <div class="flex justify-between items-center mb-6">
                <h4 class="text-sm font-bold text-white">Clip: ${jobId.substring(0,8)}...</h4>
                <span class="bg-gray-800 text-gray-400 border border-gray-700 px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider" id="badge-${jobId}">QUEUED</span>
            </div>
            
            <div class="relative px-2 md:px-6 mb-8 mt-4">
                <div class="absolute top-4 left-[10%] right-[10%] h-[3px] bg-gray-800 z-0"></div>
                <div class="absolute top-4 left-[10%] h-[3px] bg-primary z-0 transition-all duration-500" id="fill-line-${jobId}" style="width: 0%;"></div>

                <div class="flex justify-between relative z-10">
                    <div class="flex flex-col items-center gap-2">
                        <div class="w-9 h-9 rounded-full flex items-center justify-center transition-all duration-300 bg-gray-800 text-gray-500" id="step1-icon-${jobId}">
                            <i class="fa-solid fa-cloud-arrow-down text-sm"></i>
                        </div>
                        <div class="text-center"><div class="text-[11px] font-bold text-gray-500" id="step1-title-${jobId}">Downloading</div><div class="text-[10px] text-gray-600" id="step1-pct-${jobId}">--%</div></div>
                    </div>
                    <div class="flex flex-col items-center gap-2">
                        <div class="w-9 h-9 rounded-full flex items-center justify-center transition-all duration-300 bg-gray-800 text-gray-500" id="step2-icon-${jobId}">
                            <i class="fa-solid fa-scissors text-sm"></i>
                        </div>
                        <div class="text-center"><div class="text-[11px] font-bold text-gray-500" id="step2-title-${jobId}">AI Analysis</div><div class="text-[10px] text-gray-600" id="step2-pct-${jobId}">--%</div></div>
                    </div>
                    <div class="flex flex-col items-center gap-2">
                        <div class="w-9 h-9 rounded-full flex items-center justify-center transition-all duration-300 bg-gray-800 text-gray-500" id="step3-icon-${jobId}">
                            <i class="fa-solid fa-wand-magic-sparkles text-sm"></i>
                        </div>
                        <div class="text-center"><div class="text-[11px] font-bold text-gray-500" id="step3-title-${jobId}">Effects</div><div class="text-[10px] text-gray-600" id="step3-pct-${jobId}">--%</div></div>
                    </div>
                    <div class="flex flex-col items-center gap-2">
                        <div class="w-9 h-9 rounded-full flex items-center justify-center transition-all duration-300 bg-gray-800 text-gray-500" id="step4-icon-${jobId}">
                            <i class="fa-solid fa-check text-sm"></i>
                        </div>
                        <div class="text-center"><div class="text-[11px] font-bold text-gray-500" id="step4-title-${jobId}">Finished</div><div class="text-[10px] text-gray-600" id="step4-pct-${jobId}">--%</div></div>
                    </div>
                </div>
            </div>
            <div class="flex justify-between items-center text-xs border-t border-cardborder pt-3">
                <div class="text-gray-400" id="stage-${jobId}">Menunggu antrean...</div>
                <div class="text-primary font-mono" id="speed-${jobId}"></div>
            </div>
            <div id="result-${jobId}"></div>
        `;
        taskList.prepend(div);
    }

    function monitorTask(jobId) {
        if (activeStreams[jobId]) return;
        const source = new EventSource(`/api/clip/status/${jobId}`);
        activeStreams[jobId] = source;

        source.onmessage = function(event) {
            const data = JSON.parse(event.data);
            
            if (data.error || data.status === 'error') {
                const badgeEl = document.getElementById(`badge-${jobId}`);
                const stageEl = document.getElementById(`stage-${jobId}`);
                const taskEl = document.getElementById(`task-${jobId}`);
                
                if (badgeEl) {
                    badgeEl.innerText = 'ERROR';
                    badgeEl.className = 'bg-red-500/10 text-red-500 border border-red-500/20 px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider';
                }
                if (stageEl) {
                    stageEl.innerText = data.error || data.stage || 'Gagal diproses.';
                    stageEl.className = 'text-red-400 font-bold';
                }
                if (taskEl) taskEl.classList.add('border-red-500/30', 'bg-red-500/5');
                
                source.close(); 
                delete activeStreams[jobId]; 
                return;
            }

            let step = 0; let lineW = 0;
            if (data.status === 'downloading') { step = 1; lineW = 0; }
            else if (data.status === 'clipping' || data.status === 'processing') { step = 2; lineW = 33; }
            else if (data.status === 'encoding') { step = 3; lineW = 66; }
            else if (data.status === 'done') { step = 4; lineW = 100; }

            const badgeEl = document.getElementById(`badge-${jobId}`);
            const stageEl = document.getElementById(`stage-${jobId}`);
            const fillLineEl = document.getElementById(`fill-line-${jobId}`);

            if (badgeEl) badgeEl.innerText = data.status.toUpperCase();
            if (stageEl) stageEl.innerText = data.stage;
            if (fillLineEl) fillLineEl.style.width = `${lineW * 0.8}%`;

            for (let i = 1; i <= 4; i++) {
                const icon = document.getElementById(`step${i}-icon-${jobId}`);
                const title = document.getElementById(`step${i}-title-${jobId}`);
                const pct = document.getElementById(`step${i}-pct-${jobId}`);
                if (!icon || !title || !pct) continue;
                
                if (i < step) {
                    icon.className = 'w-9 h-9 rounded-full flex items-center justify-center transition-all duration-300 bg-primary text-white shadow-[0_0_15px_rgba(79,70,229,0.5)]';
                    title.className = 'text-[11px] font-bold text-white'; pct.className = 'text-[10px] text-gray-600'; pct.innerText = '100%';
                } else if (i === step) {
                    if (step === 4) {
                        icon.className = 'w-9 h-9 rounded-full flex items-center justify-center transition-all duration-300 bg-green-500 text-white shadow-[0_0_15px_rgba(34,197,94,0.4)]';
                        title.className = 'text-[11px] font-bold text-white'; pct.className = 'text-[10px] text-green-500 font-bold'; pct.innerText = 'Selesai';
                    } else {
                        icon.className = 'w-9 h-9 rounded-full flex items-center justify-center transition-all duration-300 bg-primary text-white shadow-[0_0_15px_rgba(79,70,229,0.5)]';
                        title.className = 'text-[11px] font-bold text-white'; pct.className = 'text-[10px] text-primary font-bold'; pct.innerText = `${data.progress || 0}%`;
                    }
                } else {
                    icon.className = 'w-9 h-9 rounded-full flex items-center justify-center transition-all duration-300 bg-gray-800 text-gray-500';
                    title.className = 'text-[11px] font-bold text-gray-500'; pct.className = 'text-[10px] text-gray-600'; pct.innerText = '--%';
                }
            }

            if (data.status === 'done') {
                if (badgeEl) badgeEl.className = 'bg-green-500/10 text-green-500 border border-green-500/20 px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider';
                const resEl = document.getElementById(`result-${jobId}`);
                if (resEl) {
                    resEl.innerHTML = `
                        <div class="mt-6 border-t border-cardborder pt-4 flex justify-between items-center">
                            <div class="text-xs text-gray-400">File: <span class="text-white font-medium">${data.output_file || 'clip.mp4'}</span></div>
                            <a href="/api/clip/download/${data.output_file}" target="_blank" class="bg-green-600 hover:bg-green-500 text-white px-4 py-1.5 rounded-lg text-xs font-bold transition-colors">
                                <i class="fa-solid fa-download"></i> Download
                            </a>
                        </div>`;
                }
                source.close(); delete activeStreams[jobId];
            }
        };
        source.onerror = function() { source.close(); delete activeStreams[jobId]; };
    }

    async function loadExistingJobs() {
        try {
            const res = await fetch('/api/clip/jobs');
            const jobs = await res.json();
            if (jobs && jobs.length > 0 && taskList) {
                if (taskList.innerHTML.includes('Belum ada antrean')) taskList.innerHTML = '';
                jobs.forEach(job => {
                    if (!document.getElementById(`task-${job.id}`)) {
                        createTaskElement(job.id, job.url); monitorTask(job.id);
                    }
                });
            }
        } catch (err) {}
    }
    
    window.clearHistory = async function() {
        if (!confirm('Yakin ingin menghapus semua riwayat antrean dari sistem?')) return;
        try {
            const res = await fetch('/api/clip/jobs/clear', { method: 'DELETE' });
            const data = await res.json();
            if (data.success && taskList) {
                taskList.innerHTML = '<div class="text-center py-8 text-gray-500 text-xs border-2 border-dashed border-gray-800 rounded-lg">Belum ada antrean clip saat ini.</div>';
            }
        } catch (err) { console.error('Gagal menghapus riwayat:', err); }
    };

    const galleryModal = document.getElementById('galleryModal');
    const galleryContainer = document.getElementById('galleryContainer');

    window.openGallery = async function() {
        if (galleryModal) galleryModal.classList.remove('hidden');
        await loadGalleryData();
    };

    window.closeGallery = function() { if (galleryModal) galleryModal.classList.add('hidden'); };

    async function loadGalleryData() {
        if (!galleryContainer) return;
        galleryContainer.innerHTML = '<div class="col-span-full text-center py-10 text-gray-500"><i class="fa-solid fa-spinner fa-spin text-2xl mb-2"></i><br>Memuat file...</div>';
        try {
            const res = await fetch('/api/clip/gallery');
            const json = await res.json();
            if (json.success && json.data.length > 0) {
                galleryContainer.innerHTML = json.data.map(clip => `
                    <div class="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden flex flex-col h-full shadow-lg">
                        
                        <!-- WADAH VIDEO RESPONSIVE FIX -->
                        <div class="w-full bg-black relative" style="height: 350px;">
                            <video src="${clip.url}" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; object-fit: contain;" controls preload="metadata"></video>
                        </div>
                        
                        <div class="p-4 flex-1 flex flex-col justify-between bg-gray-900/90 border-t border-gray-800">
                            <div class="text-[11px] text-gray-300 font-mono truncate mb-2" title="${clip.filename}">${clip.filename}</div>
                            <div class="flex justify-between items-center mt-auto">
                                <span class="text-[10px] bg-gray-800 text-gray-400 px-2 py-1 rounded font-bold">${clip.size}</span>
                                <div class="flex gap-2">
                                    <a href="${clip.url}" download="${clip.filename}" class="text-green-500 hover:text-green-400 bg-green-500/10 hover:bg-green-500/20 px-3 py-1.5 rounded transition-colors flex items-center gap-1 text-[11px] font-bold">
                                        <i class="fa-solid fa-download"></i> Unduh
                                    </a>
                                    <button onclick="deleteSingleClip('${clip.filename}')" class="text-red-500 hover:text-red-400 bg-red-500/10 hover:bg-red-500/20 px-2 py-1.5 rounded transition-colors">
                                        <i class="fa-solid fa-trash"></i>
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                `).join('');
            } else {
                galleryContainer.innerHTML = '<div class="col-span-full text-center py-12 text-gray-500 border-2 border-dashed border-gray-800 rounded-xl"><i class="fa-solid fa-folder-open text-3xl mb-2 opacity-50"></i><br>Galeri masih kosong.<br><span class="text-[10px]">Video yang sukses di-render akan muncul di sini.</span></div>';
            }
        } catch (err) { galleryContainer.innerHTML = `<div class="col-span-full text-center py-10 text-red-500">Gagal memuat galeri: ${err.message}</div>`; }
    }

    window.deleteSingleClip = async function(filename) {
        if (!confirm(`Yakin ingin menghapus ${filename} secara permanen?`)) return;
        try {
            const res = await fetch(`/api/clip/gallery/${filename}`, { method: 'DELETE' });
            const data = await res.json();
            if (data.success) loadGalleryData(); 
            else alert(data.error);
        } catch (err) { alert('Error: ' + err.message); }
    };

    window.deleteAllClips = async function() {
        if (!confirm('PERINGATAN! Ini akan menghapus SEMUA hasil clip di server.\nYakin ingin melanjutkan?')) return;
        try {
            const res = await fetch('/api/clip/gallery/all', { method: 'DELETE' });
            const data = await res.json();
            if (data.success) { alert(data.message); loadGalleryData(); }
        } catch (err) { alert('Error: ' + err.message); }
    };

    loadExistingJobs();
});