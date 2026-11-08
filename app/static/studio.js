

/* ---------------- GLOBAL KAGGLE WORKER POOL ---------------- */

async function refreshKaggleWorkers() {
    const container = document.getElementById("kaggle-worker-list");
    if (!container) return;
    try {
        const data = await api("/api/kaggle/workers");
        const workers = data.workers || [];
        container.innerHTML = "";
        const summary = document.createElement("p");
        summary.className = "message";
        summary.textContent = `${workers.length} / ${data.limit || 5} workers registered · one active scene per worker`;
        container.appendChild(summary);

        if (!workers.length) {
            const empty = document.createElement("p");
            empty.className = "empty";
            empty.textContent = "No Kaggle workers registered yet.";
            container.appendChild(empty);
            return;
        }

        for (const worker of workers) {
            const card = document.createElement("div");
            card.className = "kaggle-worker-card";
            const info = document.createElement("div");
            const name = document.createElement("strong");
            name.textContent = worker.name;
            const url = document.createElement("small");
            url.textContent = worker.api_url;
            const workflow = document.createElement("small");
            workflow.textContent = `Workflow: ${worker.workflow_id}`;
            info.append(name, url, workflow);
            const status = document.createElement("span");
            status.className = `worker-status worker-${String(worker.status).toLowerCase()}`;
            status.textContent = worker.status;
            card.append(info, status);
            if (worker.current_scene_id || worker.current_job_id) {
                const job = document.createElement("small");
                job.textContent = `Project ${worker.current_project_id || ""} · ${worker.current_scene_id || ""} · Job ${worker.current_job_id || "submitting"}`;
                card.appendChild(job);
            }
            if (worker.last_error) {
                const error = document.createElement("small");
                error.className = "worker-error";
                error.textContent = worker.last_error;
                card.appendChild(error);
            }
            const remove = createButton("Remove", async button => {
                button.disabled = true;
                try {
                    await api(`/api/kaggle/workers/${worker.id}`, { method: "DELETE" });
                    await refreshKaggleWorkers();
                } catch (err) {
                    button.disabled = false;
                    const message = document.getElementById("kaggle-worker-message");
                    if (message) message.textContent = err.message;
                }
            });
            remove.className = "secondary";
            card.appendChild(remove);
            container.appendChild(card);
        }
    } catch (error) {
        container.textContent = error.message;
    }
}

function initializeKaggleWorkerForm() {
    const form = document.getElementById("kaggle-worker-form");
    if (!form) return;
    form.addEventListener("submit", async event => {
        event.preventDefault();
        const message = document.getElementById("kaggle-worker-message");
        const button = form.querySelector('button[type="submit"]');
        const values = new FormData(form);
        button.disabled = true;
        button.textContent = "Checking server and registering workflow…";
        if (message) message.textContent = "";
        try {
            await api("/api/kaggle/workers/register", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name: values.get("name"),
                    api_url: values.get("api_url"),
                    api_key: values.get("api_key")
                })
            });
            form.reset();
            if (message) message.textContent = "Kaggle worker registered successfully.";
            await refreshKaggleWorkers();
        } catch (error) {
            if (message) message.textContent = error.message;
        } finally {
            button.disabled = false;
            button.textContent = "Register Kaggle Workflow";
        }
    });
    refreshKaggleWorkers();
    setInterval(refreshKaggleWorkers, 15000);
}


const form = document.getElementById("project-form");
const message = document.getElementById("message");
const list = document.getElementById("projects");

let audioPollingEnabled = false;
let audioPollInFlight = false;

const ACTIVE_STATUSES = [
    "running",
    "pause_requested",
    "stop_requested"
];

async function api(url, options = {}) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
        throw new Error(data.detail || data.message || "Request failed");
    }

    return data;
}

function createButton(label, handler) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", () => handler(button));
    return button;
}

function setMessage(container, text) {
    const p = document.createElement("p");
    p.className = "message";
    p.textContent = text;
    container.appendChild(p);
}

function isActive(status) {
    return ACTIVE_STATUSES.includes(status);
}

let projectsCache = [];
let activeProjectId = null;
let activeProjectSection = "screenplay";

const PROJECT_SECTIONS = [
    ["screenplay", "Screenplay", "▤"],
    ["images", "Images", "▧"],
    ["audio", "Audio", "♫"],
    ["video-clips", "Video Clips", "▶"],
    ["story-video", "Story Video", "▣"]
];

function setPage(page, label) {
    document.querySelectorAll(".page-view").forEach(view => {
        view.hidden = view.dataset.page !== page;
    });
    document.querySelectorAll("[data-page-link]").forEach(link => {
        link.classList.toggle("active", link.dataset.pageLink === page);
    });
    document.querySelectorAll(".project-nav-item").forEach(item => {
        item.classList.toggle("is-active", page === "project" && String(item.dataset.projectId) === String(activeProjectId));
    });
    const breadcrumb = document.getElementById("breadcrumb-current");
    if (breadcrumb) breadcrumb.textContent = label || ({
        overview: "Overview", create: "New story", workers: "Render workers"
    }[page] || "Project");
}

function selectProject(projectId, section = "screenplay") {
    activeProjectId = Number(projectId);
    activeProjectSection = section;
    const project = projectsCache.find(item => Number(item.id) === activeProjectId);
    if (!project) return;
    const sectionLabel = PROJECT_SECTIONS.find(item => item[0] === section)?.[1] || "Screenplay";
    setPage("project", project.title || `Project ${project.id}`);
    renderSelectedProjectView();
    document.querySelectorAll(".project-nav-item").forEach(item => {
        item.classList.toggle("is-active", String(item.dataset.projectId) === String(activeProjectId));
        item.querySelectorAll(".project-submenu button").forEach(button => {
            button.classList.toggle("active", button.dataset.section === section);
        });
    });
    const breadcrumb = document.getElementById("breadcrumb-current");
    if (breadcrumb) breadcrumb.textContent = `${project.title || "Project"} / ${sectionLabel}`;
}

function renderProjectNavigation() {
    const container = document.getElementById("project-nav-list");
    if (!container) return;
    container.innerHTML = "";
    if (!projectsCache.length) {
        const empty = document.createElement("p");
        empty.className = "project-nav-empty";
        empty.textContent = "No projects yet";
        container.appendChild(empty);
        return;
    }

    for (const project of projectsCache) {
        const item = document.createElement("div");
        item.className = "project-nav-item";
        item.dataset.projectId = project.id;

        const projectButton = document.createElement("button");
        projectButton.type = "button";
        projectButton.className = "project-nav-project";
        projectButton.title = project.title || `Project ${project.id}`;

        const name = document.createElement("span");
        name.className = "project-name";
        name.textContent = project.title || `Project ${project.id}`;
        const arrow = document.createElement("span");
        arrow.className = "project-arrow";
        arrow.textContent = "›";
        projectButton.append(name, arrow);
        projectButton.addEventListener("click", () => selectProject(project.id, "screenplay"));

        const submenu = document.createElement("div");
        submenu.className = "project-submenu";
        for (const [key, label, mark] of PROJECT_SECTIONS) {
            const button = document.createElement("button");
            button.type = "button";
            button.dataset.section = key;
            const icon = document.createElement("span");
            icon.className = "submenu-mark";
            icon.textContent = mark;
            const text = document.createElement("span");
            text.textContent = label;
            button.append(icon, text);
            button.addEventListener("click", event => {
                event.stopPropagation();
                selectProject(project.id, key);
            });
            submenu.appendChild(button);
        }
        item.append(projectButton, submenu);
        container.appendChild(item);
    }
}

async function loadProjects() {
    const container = document.getElementById("projects");
    try {
        projectsCache = await api("/api/projects");
        if (!Array.isArray(projectsCache)) projectsCache = [];
        renderProjectNavigation();

        if (!projectsCache.length) {
            activeProjectId = null;
            if (container) container.textContent = "No projects yet.";
            if (document.getElementById("project-detail-view")?.hidden === false) {
                setPage("overview");
            }
            return;
        }

        if (!projectsCache.some(item => Number(item.id) === Number(activeProjectId))) {
            activeProjectId = null;
        }
        if (activeProjectId !== null) {
            await renderSelectedProjectView();
        }
    } catch (error) {
        if (container) container.textContent = error.message;
        const nav = document.getElementById("project-nav-list");
        if (nav) nav.textContent = error.message;
    }
}

function addProjectPanel(title, description = "") {
    const root = document.getElementById("project-detail-content");
    root.innerHTML = "";

    const project = projectsCache.find(item => Number(item.id) === Number(activeProjectId));
    if (!project) return null;

    const heading = document.createElement("div");
    heading.className = "project-detail-heading";
    const copy = document.createElement("div");
    const eyebrow = document.createElement("div");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = "PROJECT WORKSPACE";
    const h1 = document.createElement("h1");
    h1.textContent = project.title || `Project ${project.id}`;
    const p = document.createElement("p");
    p.textContent = `${project.target_duration_minutes || "—"} min · ${project.language || "Language not set"} · ${project.status || "Draft"}`;
    copy.append(eyebrow, h1, p);
    heading.appendChild(copy);
    root.appendChild(heading);

    const tabs = document.createElement("div");
    tabs.className = "project-stage-tabs";
    for (const [key, label] of PROJECT_SECTIONS) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.classList.toggle("active", key === activeProjectSection);
        button.addEventListener("click", () => selectProject(project.id, key));
        tabs.appendChild(button);
    }
    root.appendChild(tabs);

    const panel = document.createElement("section");
    panel.className = "project-content-panel";
    root.appendChild(panel);
    return { project, panel };
}

async function getProjectStages(projectId) {
    const stages = await api(`/api/projects/${projectId}/stages`);
    return Object.fromEntries(stages.map(stage => [stage.stage_key, stage]));
}

async function renderSelectedProjectView() {
    const selected = addProjectPanel(
        PROJECT_SECTIONS.find(item => item[0] === activeProjectSection)?.[1] || "Screenplay"
    );
    if (!selected) return;
    const { project, panel } = selected;
    const projectId = project.id;
    const heading = document.createElement("h3");
    heading.textContent = PROJECT_SECTIONS.find(item => item[0] === activeProjectSection)?.[1] || "Screenplay";
    panel.appendChild(heading);

    try {
        const stages = await getProjectStages(projectId);
        if (activeProjectSection === "screenplay") {
            await renderScreenplayView(panel, project, stages.screenplay);
        } else if (activeProjectSection === "images") {
            await renderImagesView(panel, project, stages);
        } else if (activeProjectSection === "audio") {
            await renderAudioStage(panel, project, stages.audio);
        } else if (activeProjectSection === "video-clips") {
            await renderVideoStage(panel, project, stages.video);
        } else if (activeProjectSection === "story-video") {
            await renderStoryVideoView(panel, project);
        }
    } catch (error) {
        setMessage(panel, `Could not load this project section: ${error.message}`);
    }
}

async function renderScreenplayView(panel, project, stage) {
    let data = null;
    try {
        data = await api(`/api/projects/${project.id}/screenplay`);
    } catch (error) {
        setMessage(panel, `Could not load the saved screenplay: ${error.message}`);
        return;
    }

    const screenplay = data?.screenplay || null;
    const status = stage?.status || "pending";
    if (!screenplay) {
        const detail = document.createElement("p");
        detail.className = "message";
        detail.textContent = stage?.detail || "This project does not have a saved screenplay yet.";
        panel.appendChild(detail);
        const button = createButton(
            status === "running" ? "Screenplay is generating…" : "Generate screenplay",
            async btn => {
                btn.disabled = true;
                btn.textContent = "Generating screenplay…";
                try {
                    await api(`/api/projects/${project.id}/screenplay/generate`, { method: "POST" });
                    await loadProjects();
                } catch (error) {
                    btn.disabled = false;
                    btn.textContent = "Generate screenplay";
                    setMessage(panel, error.message);
                }
            }
        );
        button.disabled = status === "running";
        button.className = "primary-button";
        panel.appendChild(button);
        return;
    }

    if (screenplay.logline) {
        const logline = document.createElement("p");
        logline.className = "screenplay-logline";
        logline.textContent = screenplay.logline;
        panel.appendChild(logline);
    }
    if (screenplay.characters?.length) {
        const sub = document.createElement("h4");
        sub.className = "section-subheading";
        sub.textContent = "Characters";
        panel.appendChild(sub);
        screenplay.characters.forEach(character => {
            const p = document.createElement("p");
            p.textContent = `${character.name || character.character_id}: ${character.description || ""}`;
            panel.appendChild(p);
        });
    }
    const scenesTitle = document.createElement("h4");
    scenesTitle.className = "section-subheading";
    scenesTitle.textContent = `Scenes (${(screenplay.scenes || []).length})`;
    panel.appendChild(scenesTitle);
    (screenplay.scenes || []).forEach((scene, index) => {
        const card = document.createElement("div");
        card.className = "screenplay-scene";
        const title = document.createElement("h4");
        title.textContent = `${scene.scene_id || `Scene ${index + 1}`} · ${scene.title || scene.location || ""}`;
        const summary = document.createElement("p");
        summary.textContent = scene.summary || scene.description || "";
        card.append(title, summary);
        const dialogueLines = scene.dialogue_and_narration || scene.dialogue || [];
        if (dialogueLines.length) {
            const dialogue = document.createElement("p");
            dialogue.textContent = dialogueLines.map(line => `${line.speaker || "Narrator"}: ${line.text || ""}`).join("\n");
            card.appendChild(dialogue);
        }
        panel.appendChild(card);
    });

    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "View complete screenplay JSON";
    const pre = document.createElement("pre");
    pre.className = "screenplay-json";
    pre.textContent = JSON.stringify(screenplay, null, 2);
    details.append(summary, pre);
    panel.appendChild(details);

    if (status === "waiting_approval") {
        const approve = createButton("Approve screenplay", async btn => {
            btn.disabled = true;
            try {
                await api(`/api/projects/${project.id}/screenplay/approve`, { method: "POST" });
                await loadProjects();
            } catch (error) {
                btn.disabled = false;
                setMessage(panel, error.message);
            }
        });
        approve.className = "primary-button";
        panel.appendChild(approve);
    } else if (status === "completed") {
        const approved = document.createElement("p");
        approved.className = "message";
        approved.textContent = "Screenplay approved. Continue with Images when ready.";
        panel.appendChild(approved);
    }
}

function addImageAssetCard(container, projectId, title, record) {
    const card = document.createElement("div");
    card.className = "image-asset-card";
    const path = record?.path;
    if (path && record.status === "completed") {
        const image = document.createElement("img");
        image.loading = "lazy";
        image.src = `/api/projects/${projectId}/assets/${path}`;
        image.alt = title;
        card.appendChild(image);
    }
    const info = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = title;
    const status = document.createElement("small");
    status.textContent = record?.status || "pending";
    info.append(name, status);
    if (record?.error) {
        const error = document.createElement("small");
        error.textContent = record.error;
        info.appendChild(error);
    }
    card.appendChild(info);
    container.appendChild(card);
}

async function renderImagesView(panel, project, stages) {
    const imageResponse = await api(`/api/projects/${project.id}/images/status`).catch(() => null);
    const status = imageResponse?.status || imageResponse || null;
    const refs = status?.references || {};
    const sceneImages = status?.scenes || {};
    const refStatus = refs.status || stages.character_bible?.status || "pending";
    const sceneStatus = stages.scene_images?.status || "pending";

    const summary = document.createElement("p");
    summary.className = "message";
    const stats = status?.statistics || {};
    summary.textContent = status
        ? `Image status: ${status.status || "pending"} · References: ${stats.references_completed || 0}/${stats.references_total || 0} · Scene images: ${stats.scenes_completed || 0}/${stats.scenes_total || 0}`
        : "No image manifest has been created yet.";
    panel.appendChild(summary);

    if (refStatus !== "completed") {
        const button = createButton(
            refStatus === "running" ? "Generating references…" : "Generate character, prop & location references",
            async btn => {
                btn.disabled = true;
                try {
                    await api(`/api/projects/${project.id}/references/generate`, { method: "POST" });
                    await loadProjects();
                } catch (error) {
                    btn.disabled = false;
                    setMessage(panel, error.message);
                }
            }
        );
        button.disabled = refStatus === "running";
        button.className = "primary-button";
        panel.appendChild(button);
    }

    const groups = [
        ["characters", "Character references"],
        ["props", "Prop references"],
        ["locations", "Location references"]
    ];
    for (const [key, label] of groups) {
        const records = Object.entries(refs[key] || {});
        if (!records.length) continue;
        const title = document.createElement("h4");
        title.className = "section-subheading";
        title.textContent = label;
        panel.appendChild(title);
        const grid = document.createElement("div");
        grid.className = "image-asset-grid";
        records.forEach(([id, record]) => addImageAssetCard(grid, project.id, record.name || id, record));
        panel.appendChild(grid);
    }

    if (refStatus === "completed" && sceneStatus !== "completed") {
        const button = createButton(
            sceneStatus === "running" ? "Generating scene images…" : "Generate scene images",
            async btn => {
                btn.disabled = true;
                try {
                    await api(`/api/projects/${project.id}/images/generate`, { method: "POST" });
                    await loadProjects();
                } catch (error) {
                    btn.disabled = false;
                    setMessage(panel, error.message);
                }
            }
        );
        button.disabled = sceneStatus === "running";
        button.className = "primary-button";
        panel.appendChild(button);
    }

    const sceneRecords = Object.entries(sceneImages);
    if (sceneRecords.length) {
        const title = document.createElement("h4");
        title.className = "section-subheading";
        title.textContent = "Scene images";
        panel.appendChild(title);
        const grid = document.createElement("div");
        grid.className = "image-asset-grid";
        sceneRecords.forEach(([id, record]) => addImageAssetCard(grid, project.id, id, record));
        panel.appendChild(grid);
    }
}

async function renderStoryVideoView(panel, project) {
    const manifest = await api(`/api/projects/${project.id}/video`).catch(() => null);
    const finalPath = manifest?.final_video || manifest?.story_video || manifest?.output;
    if (finalPath && manifest?.status === "completed") {
        const video = document.createElement("video");
        video.controls = true;
        video.preload = "metadata";
        video.src = `/api/projects/${project.id}/assets/${finalPath}`;
        video.className = "story-video-player";
        video.style.width = "100%";
        video.style.maxWidth = "1000px";
        panel.appendChild(video);
        const link = document.createElement("a");
        link.href = video.src;
        link.textContent = "Open / download final story video";
        link.target = "_blank";
        panel.appendChild(link);
        return;
    }
    const empty = document.createElement("div");
    empty.className = "story-video-empty";
    const strong = document.createElement("strong");
    strong.textContent = "Your final story video will appear here";
    const p = document.createElement("p");
    p.textContent = "First complete the scene videos in Video Clips. The current backend generates and stores individual scene clips; final film assembly is not yet available in this project workflow.";
    empty.append(strong, p);
    panel.appendChild(empty);
    const button = createButton("View video clips", () => selectProject(project.id, "video-clips"));
    button.className = "secondary-button";
    panel.appendChild(button);
}

document.querySelectorAll("[data-page-link]").forEach(button => {
    button.addEventListener("click", () => {
        const page = button.dataset.pageLink;
        setPage(page);
    });
});
const projectToggle = document.getElementById("my-projects-toggle");
if (projectToggle) {
    projectToggle.addEventListener("click", () => {
        const nav = document.getElementById("project-nav");
        const open = nav.classList.toggle("is-open");
        projectToggle.setAttribute("aria-expanded", String(open));
    });
}
document.querySelectorAll("[data-project-shortcut]").forEach(button => {
    button.addEventListener("click", () => {
        if (projectsCache.length) selectProject(projectsCache[0].id, "screenplay");
        else setPage("create");
    });
});
async function runAction(button, projectId, endpoint, progressText) {
    button.disabled = true;
    const originalText = button.textContent;
    button.textContent = progressText;

    try {
        await api(`/api/projects/${projectId}/${endpoint}`, {
            method: "POST"
        });
        await loadProjects();
    } catch (error) {
        button.disabled = false;
        button.textContent = originalText;

        const card = button.closest(".project") || document.getElementById("project-detail-content");
        if (card) setMessage(card, error.message);
    }
}

if (form) {
    form.addEventListener("submit", async event => {
        event.preventDefault();

        if (message) message.textContent = "Creating project...";

        const formData = new FormData(form);
        const payload = Object.fromEntries(formData.entries());

        payload.target_duration_minutes =
            Number(payload.target_duration_minutes);

        try {
            const data = await api("/api/projects", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });

            if (message) message.textContent = `Created project #${data.id}`;

            form.reset();
            activeProjectId = Number(data.id);
            activeProjectSection = "screenplay";
            await loadProjects();
            selectProject(data.id, "screenplay");

        } catch (error) {
            if (message) message.textContent = error.message;
        }
    });
}

/* ---------------- AUDIO STAGE ---------------- */

async function renderAudioStage(workflow, project, audioStage) {
    const panel = document.createElement("div");
    panel.className = "audio-stage";
    panel.dataset.projectId = project.id;

    const heading = document.createElement("h4");
    heading.textContent = "Audio Production · Voice Assignment";
    panel.appendChild(heading);

    let screenplayData;
    let profiles;
    let manifest = null;

    try {
        [screenplayData, profiles] = await Promise.all([
            api(`/api/projects/${project.id}/screenplay`),
            api("/api/voicebox/profiles")
        ]);

        manifest = await api(
            `/api/projects/${project.id}/audio`
        ).catch(() => null);

    } catch (error) {
        setMessage(panel, `Audio setup unavailable: ${error.message}`);
        workflow.appendChild(panel);
        return;
    }

    const screenplay = screenplayData.screenplay || null;
    if (!screenplay) {
        setMessage(panel, "Generate the screenplay first. Once it is saved, voice assignments and generated audio will appear here.");
        workflow.appendChild(panel);
        return;
    }
    const characters = screenplay.characters || [];

    const speakers = characters.map(character => ({
        id: character.character_id,
        name: character.name || character.character_id,
        kind: "character"
    })).filter(item => item.id);

    speakers.push({
        id: "NARRATOR",
        name: "Narrator / Voice-over",
        kind: "narrator"
    });

    const hindiProfiles = profiles.filter(profile =>
        String(profile.language || "").toLowerCase() === "hi" ||
        String(profile.language || "").toLowerCase() === "hindi"
    );

    const available = hindiProfiles.length ? hindiProfiles : profiles;

    if (!available.length) {
        setMessage(panel, "Voicebox returned no available voice profiles.");
        workflow.appendChild(panel);
        return;
    }

    const savedAssignments = Object.fromEntries(
        (manifest?.voice_assignments || []).map(item => [
            item.speaker_id,
            item.profile_id
        ])
    );

    const selectors = {};
    const assignmentSection = document.createElement("div");
    assignmentSection.className = "voice-assignment-list";

    const assignmentHeading = document.createElement("p");
    assignmentHeading.textContent =
        "Choose a separate voice profile for every character and for narration:";

    assignmentSection.appendChild(assignmentHeading);

    for (const speaker of speakers) {
        const label = document.createElement("label");
        label.className = "voice-assignment";

        const title = document.createElement("span");
        title.textContent = speaker.kind === "narrator"
            ? speaker.name
            : `${speaker.name} (${speaker.id})`;

        const select = document.createElement("select");
        select.dataset.speakerId = speaker.id;

        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "Select voice profile…";
        select.appendChild(placeholder);

        for (const profile of available) {
            const option = document.createElement("option");
            option.value = profile.id;

            option.textContent =
                `${profile.name} · ${profile.language || "language not specified"}` +
                `${profile.default_engine ? " · " + profile.default_engine : ""}`;

            if (
                String(profile.id) ===
                String(savedAssignments[speaker.id] || "")
            ) {
                option.selected = true;
            } else if (
                !savedAssignments[speaker.id] &&
                speaker.kind === "narrator" &&
                String(profile.name).toLowerCase() === "niya"
            ) {
                option.selected = true;
            }

            select.appendChild(option);
        }

        selectors[speaker.id] = select;
        label.append(title, select);
        assignmentSection.appendChild(label);
    }

    panel.appendChild(assignmentSection);

    const status = manifest?.status || audioStage?.status || "pending";
    panel.dataset.audioStatus = status;

    if (isActive(status)) audioPollingEnabled = true;

    const completed = Number(manifest?.completed || 0);
    const total = Number(
        manifest?.total || manifest?.segments?.length || 0
    );

    const detail = document.createElement("p");
    detail.className = "message";
    detail.dataset.role = "audio-detail";
    detail.textContent = manifest?.detail || audioStage?.detail ||
        "Assign the voices, then generate the audio.";
    panel.appendChild(detail);

    const progress = document.createElement("p");
    progress.dataset.role = "audio-progress";
    progress.textContent = total > 0
        ? `Progress: ${completed} / ${total} segments`
        : "Progress: waiting for audio segments";
    progress.hidden = total <= 0;
    panel.appendChild(progress);

    const current = document.createElement("p");
    current.dataset.role = "audio-current";

    if (manifest?.current) {
        current.textContent =
            `Current operation: ${manifest.current.scene_id} · ${manifest.current.segment_id} · ${manifest.current.speaker}`;
    } else {
        current.hidden = true;
    }

    panel.appendChild(current);

    const errorMessage = document.createElement("p");
    errorMessage.className = "message";
    errorMessage.dataset.role = "audio-error";
    errorMessage.textContent = manifest?.error || "";
    errorMessage.hidden = !manifest?.error;
    panel.appendChild(errorMessage);

    const controls = document.createElement("div");
    controls.className = "audio-controls";
    panel.appendChild(controls);

    const showGeneratedAssets = () => {
        const scenes = manifest?.scenes || [];

        for (const scene of scenes) {
            const section = document.createElement("div");
            section.className = "audio-scene";

            const title = document.createElement("p");
            title.textContent = scene.scene_id;
            section.appendChild(title);

            if (scene.audio) {
                const audio = document.createElement("audio");
                audio.controls = true;
                audio.preload = "none";
                audio.src =
                    `/api/projects/${project.id}/assets/${scene.audio}`;
                audio.style.display = "block";
                audio.style.width = "100%";
                section.appendChild(audio);
            }

            for (const segment of (scene.segments || [])) {
                if (!segment.audio) continue;

                const link = document.createElement("a");
                link.href =
                    `/api/projects/${project.id}/assets/${segment.audio}`;
                link.textContent =
                    `Download ${segment.segment_id} · ${segment.speaker}`;
                link.target = "_blank";
                link.style.display = "block";
                section.appendChild(link);
            }

            panel.appendChild(section);
        }

        if (manifest?.segments?.length) {
            const completedSegments = manifest.segments.filter(
                item => item.status === "completed"
            );

            if (completedSegments.length && !scenes.length) {
                const listTitle = document.createElement("p");
                listTitle.textContent = "Completed audio segments:";
                panel.appendChild(listTitle);

                for (const segment of completedSegments) {
                    const link = document.createElement("a");
                    link.href =
                        `/api/projects/${project.id}/assets/${segment.audio}`;
                    link.textContent =
                        `Download ${segment.segment_id} · ${segment.speaker}`;
                    link.target = "_blank";
                    link.style.display = "block";
                    panel.appendChild(link);
                }
            }
        }
    };

    const addControl = (label, endpoint, cssClass) => {
        const button = createButton(label, async btn => {
            btn.disabled = true;

            try {
                await api(
                    `/api/projects/${project.id}/audio/${endpoint}`,
                    { method: "POST" }
                );
                await loadProjects();
            } catch (error) {
                btn.disabled = false;
                setMessage(panel, error.message);
            }
        });

        if (cssClass) button.className = cssClass;
        controls.appendChild(button);
    };

    if (isActive(status)) {
        assignmentSection.querySelectorAll("select").forEach(
            select => select.disabled = true
        );

        addControl(
            status === "running"
                ? "Pause after current segment"
                : "Pause requested…",
            "control/pause",
            "secondary"
        );

        addControl(
            status === "running"
                ? "Stop after current segment"
                : "Stop requested…",
            "control/stop",
            "danger"
        );

    } else {
        const generateButton = createButton(
            manifest ? "Generate / Continue Audio" : "Generate Scene Audio",
            async button => {
                const voiceAssignments = {};

                for (const [speakerId, select] of Object.entries(selectors)) {
                    if (!select.value) {
                        setMessage(
                            panel,
                            `Please select a voice profile for ${speakerId}.`
                        );
                        select.focus();
                        return;
                    }

                    voiceAssignments[speakerId] = select.value;
                }

                button.disabled = true;
                button.textContent = "Starting audio generation…";

                try {
                    await api(
                        `/api/projects/${project.id}/audio/generate`,
                        {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({
                                voice_assignments: voiceAssignments,
                                engine: "chatterbox"
                            })
                        }
                    );

                    await loadProjects();

                } catch (error) {
                    button.disabled = false;
                    button.textContent = manifest
                        ? "Generate / Continue Audio"
                        : "Generate Scene Audio";

                    setMessage(panel, error.message);
                }
            }
        );

        controls.appendChild(generateButton);

        if (
            manifest &&
            ["paused", "stopped", "failed"].includes(status)
        ) {
            addControl("Resume from checkpoint", "resume", "secondary");
        }
    }

    if (
        status === "completed" ||
        (manifest?.segments || []).some(
            item => item.status === "completed"
        )
    ) {
        showGeneratedAssets();
    }

    workflow.appendChild(panel);
}

/* ---------------- VIDEO STAGE ---------------- */

function videoStatusLabel(status) {
    const labels = {
        completed: "Completed",
        running: "Generating",
        pending: "Pending",
        failed: "Failed",
        paused: "Paused",
        stopped: "Stopped",
        pause_requested: "Pause requested",
        stop_requested: "Stop requested"
    };

    return labels[status] || status || "Pending";
}

function getScreenplaySceneRecords(screenplay) {
    const candidates = [
        screenplay?.scenes,
        screenplay?.scene_list,
        screenplay?.storyboard?.scenes,
        screenplay?.shots
    ];

    const scenes = candidates.find(Array.isArray) || [];

    return scenes.map((scene, index) => {
        const sceneId =
            scene.scene_id ??
            scene.id ??
            scene.scene_number ??
            scene.number ??
            `scene_${index + 1}`;

        return {
            ...scene,
            scene_id: String(sceneId),
            status: "pending",
            video: null,
            error: null
        };
    });
}

function normalizeVideoRecords(manifest) {
    const raw = manifest?.scenes;

    if (Array.isArray(raw)) return raw;

    if (raw && typeof raw === "object") {
        return Object.entries(raw).map(([sceneId, scene]) => ({
            ...scene,
            scene_id: scene.scene_id || sceneId,
            video: scene.video || scene.path || null
        }));
    }

    return [];
}

function mergeSceneRecords(manifestRecords, screenplayRecords) {
    if (!manifestRecords.length) return screenplayRecords;

    const manifestIds = new Set(
        manifestRecords.map(scene => String(scene.scene_id))
    );

    const extra = screenplayRecords.filter(
        scene => !manifestIds.has(String(scene.scene_id))
    );

    return [...manifestRecords, ...extra];
}

function createVideoSceneCard(project, scene, workerActive) {
    const sceneId = String(scene.scene_id);

    const section = document.createElement("div");
    section.className = "video-scene";
    section.dataset.sceneId = sceneId;

    const title = document.createElement("h5");
    title.textContent = `Scene ${sceneId}`;

    const status = document.createElement("p");
    status.className = "message video-scene-status";
    status.textContent = videoStatusLabel(scene.status);

    section.append(title, status);

    const error = document.createElement("p");
    error.className = "message video-scene-error";
    error.textContent = scene.error ? `Error: ${scene.error}` : "";
    error.hidden = !scene.error;
    section.appendChild(error);

    const videoPath = scene.video || scene.path || null;

    if (scene.status === "completed" && videoPath) {
        const video = document.createElement("video");
        video.controls = true;
        video.preload = "none";
        video.src =
            `/api/projects/${project.id}/assets/${videoPath}`;
        video.style.width = "100%";
        video.style.maxWidth = "640px";
        video.style.display = "block";
        section.appendChild(video);

        const download = document.createElement("a");
        download.href =
            `/api/projects/${project.id}/assets/${videoPath}`;
        download.textContent = "Download clip";
        download.target = "_blank";
        download.style.display = "block";
        section.appendChild(download);
    } else {
        const placeholder = document.createElement("p");
        placeholder.className = "video-scene-placeholder";

        if (scene.status === "running") {
            placeholder.textContent =
                "This scene is currently being generated.";
        } else if (scene.status === "failed") {
            placeholder.textContent =
                "Video generation failed. You can retry this scene.";
        } else {
            placeholder.textContent =
                "No completed video clip is available for this scene.";
        }

        section.appendChild(placeholder);
    }

    const retryButton = createButton(
        scene.status === "completed" && videoPath
            ? "Regenerate Clip"
            : "Generate / Retry This Clip",
        async button => {
            button.disabled = true;
            button.textContent = "Queuing scene…";

            try {
                await api(
                    `/api/projects/${project.id}/video/scenes/${encodeURIComponent(sceneId)}/regenerate`,
                    { method: "POST" }
                );

                audioPollingEnabled = true;
                await loadProjects();

            } catch (error) {
                button.disabled = false;
                button.textContent =
                    scene.status === "completed" && videoPath
                        ? "Regenerate Clip"
                        : "Generate / Retry This Clip";

                setMessage(section, error.message);
            }
        }
    );

    retryButton.className = "secondary";
    retryButton.disabled = workerActive || scene.status === "running";

    if (scene.status !== "running") {
        section.appendChild(retryButton);
    }

    return section;
}

async function renderVideoStage(workflow, project, videoStage) {
    const panel = document.createElement("div");
    panel.className = "video-stage";
    panel.dataset.projectId = project.id;

    const heading = document.createElement("h4");
    heading.textContent = "Video Production · Kaggle LTX";
    panel.appendChild(heading);

    const [manifest, screenplayData] = await Promise.all([
        api(`/api/projects/${project.id}/video`).catch(() => null),
        api(`/api/projects/${project.id}/screenplay`).catch(() => null)
    ]);

    const manifestRecords = normalizeVideoRecords(manifest);
    const screenplayRecords = getScreenplaySceneRecords(
        screenplayData?.screenplay || {}
    );

    const sceneRecords = mergeSceneRecords(
        manifestRecords,
        screenplayRecords
    );

    const status = manifest?.status || videoStage?.status || "pending";
    const workerActive = isActive(status);

    panel.dataset.videoStatus = status;

    if (workerActive) audioPollingEnabled = true;

    const detail = document.createElement("p");
    detail.className = "message";
    detail.dataset.role = "video-detail";
    detail.textContent = manifest?.detail || videoStage?.detail ||
        "Generate one LTX clip per scene using its approved image and combined audio.";
    panel.appendChild(detail);

    const completed = Number(
        manifest?.completed ??
        sceneRecords.filter(scene => scene.status === "completed").length
    );

    const total = Number(
        manifest?.total || sceneRecords.length || 0
    );

    const progress = document.createElement("p");
    progress.dataset.role = "video-progress";
    progress.textContent = total
        ? `Progress: ${completed} / ${total} scenes`
        : "Progress: waiting to start";
    panel.appendChild(progress);

    const current = document.createElement("p");
    current.dataset.role = "video-current";

    if (manifest?.current) {
        current.textContent = `Current scene: ${manifest.current.scene_id}`;
    } else {
        current.hidden = true;
    }

    panel.appendChild(current);

    const error = document.createElement("p");
    error.className = "message";
    error.dataset.role = "video-error";
    error.textContent = manifest?.error || "";
    error.hidden = !manifest?.error;
    panel.appendChild(error);

    const controls = document.createElement("div");
    controls.className = "audio-controls";
    panel.appendChild(controls);

    const addControl = (label, endpoint, cssClass) => {
        const button = createButton(label, async btn => {
            btn.disabled = true;

            try {
                await api(
                    `/api/projects/${project.id}/video/${endpoint}`,
                    { method: "POST" }
                );

                audioPollingEnabled = true;
                await loadProjects();

            } catch (err) {
                btn.disabled = false;
                setMessage(panel, err.message);
            }
        });

        if (cssClass) button.className = cssClass;
        controls.appendChild(button);
    };

    if (workerActive) {
        addControl(
            status === "running"
                ? "Pause after current clip"
                : "Pause requested…",
            "control/pause",
            "secondary"
        );

        addControl(
            status === "running"
                ? "Stop after current clip"
                : "Stop requested…",
            "control/stop",
            "danger"
        );
    } else {
        if (["paused", "stopped", "failed"].includes(status)) {
            addControl("Resume from checkpoint", "resume", "secondary");
        }

        if (
            status !== "completed" ||
            sceneRecords.some(scene =>
                scene.status !== "completed" || !(scene.video || scene.path)
            )
        ) {
            addControl("Generate / Continue Videos", "generate", "");
        }
    }

    const scenesContainer = document.createElement("div");
    scenesContainer.className = "video-scenes";
    scenesContainer.dataset.role = "video-scenes";

    if (sceneRecords.length) {
        for (const scene of sceneRecords) {
            scenesContainer.appendChild(
                createVideoSceneCard(project, scene, workerActive)
            );
        }
    } else {
        const empty = document.createElement("p");
        empty.textContent =
            "No scenes were found in the screenplay or video manifest.";
        scenesContainer.appendChild(empty);
    }

    panel.appendChild(scenesContainer);
    workflow.appendChild(panel);
}

/* ---------------- VIDEO SCENE STATUS UPDATES ---------------- */

function updateVideoSceneCards(panel, projectId, manifest) {
    const container = panel.querySelector(
        '[data-role="video-scenes"]'
    );

    if (!container) return;

    const records = normalizeVideoRecords(manifest);

    // Do not remove screenplay-derived scene cards when the manifest is empty.
    if (!records.length) {
        container.querySelectorAll(".video-scene button").forEach(button => {
            button.disabled = isActive(manifest.status);
        });
        return;
    }

    const active = isActive(manifest.status);

    const oldCards = Array.from(
        container.querySelectorAll(".video-scene")
    );

    const oldSignature = oldCards.map(card => {
        const video = card.querySelector("video");

        return [
            card.dataset.sceneId,
            card.querySelector(".video-scene-status")?.textContent || "",
            video?.getAttribute("src") || "",
            card.querySelector(".video-scene-error")?.textContent || ""
        ].join("|");
    }).join("||");

    const newSignature = records.map(scene => [
        scene.scene_id,
        videoStatusLabel(scene.status),
        scene.status === "completed" && (scene.video || scene.path)
            ? `/api/projects/${projectId}/assets/${scene.video || scene.path}`
            : "",
        scene.error ? `Error: ${scene.error}` : ""
    ].join("|")).join("||");

    if (oldSignature === newSignature) {
        container.querySelectorAll("button").forEach(button => {
            button.disabled = active;
        });
        return;
    }

    container.innerHTML = "";

    for (const scene of records) {
        container.appendChild(
            createVideoSceneCard(
                { id: projectId },
                scene,
                active
            )
        );
    }
}

/* ---------------- STATUS POLLING ---------------- */

setInterval(async () => {
    if (
        !audioPollingEnabled ||
        document.visibilityState !== "visible" ||
        audioPollInFlight
    ) {
        return;
    }

    const panels = Array.from(
        document.querySelectorAll(".audio-stage")
    );

    const videoPanels = Array.from(
        document.querySelectorAll(".video-stage")
    );

    if (!panels.length && !videoPanels.length) {
        audioPollingEnabled = false;
        return;
    }

    audioPollInFlight = true;

    try {
        // AUDIO STATUS POLLING
        for (const panel of panels) {
            const projectId = panel.dataset.projectId;
            if (!projectId) continue;

            const manifest = await api(
                `/api/projects/${projectId}/audio`
            ).catch(() => null);

            if (!manifest) continue;

            const detail = panel.querySelector(
                '[data-role="audio-detail"]'
            );

            const progress = panel.querySelector(
                '[data-role="audio-progress"]'
            );

            const current = panel.querySelector(
                '[data-role="audio-current"]'
            );

            const errorMessage = panel.querySelector(
                '[data-role="audio-error"]'
            );

            if (detail) {
                detail.textContent =
                    manifest.detail || manifest.status || "";
            }

            if (progress) {
                const completed = Number(manifest.completed || 0);
                const total = Number(
                    manifest.total || manifest.segments?.length || 0
                );

                progress.textContent =
                    `Progress: ${completed} / ${total} segments`;
                progress.hidden = total <= 0;
            }

            if (current) {
                if (manifest.current) {
                    current.textContent =
                        `Current operation: ${manifest.current.scene_id} · ${manifest.current.segment_id} · ${manifest.current.speaker}`;
                    current.hidden = false;
                } else {
                    current.textContent = "";
                    current.hidden = true;
                }
            }

            if (errorMessage) {
                errorMessage.textContent = manifest.error || "";
                errorMessage.hidden = !manifest.error;
            }

            const previousStatus = panel.dataset.audioStatus;
            const nextStatus = manifest.status || previousStatus;

            panel.dataset.audioStatus = nextStatus;

            if (isActive(previousStatus) && !isActive(nextStatus)) {
                await loadProjects();
                return;
            }
        }

        // VIDEO STATUS POLLING
        for (const panel of videoPanels) {
            const projectId = panel.dataset.projectId;
            if (!projectId) continue;

            const manifest = await api(
                `/api/projects/${projectId}/video`
            ).catch(() => null);

            if (!manifest) continue;

            const detail = panel.querySelector(
                '[data-role="video-detail"]'
            );

            const progress = panel.querySelector(
                '[data-role="video-progress"]'
            );

            const current = panel.querySelector(
                '[data-role="video-current"]'
            );

            const error = panel.querySelector(
                '[data-role="video-error"]'
            );

            if (detail) {
                detail.textContent =
                    manifest.detail || manifest.status || "";
            }

            if (progress) {
                progress.textContent =
                    `Progress: ${Number(manifest.completed || 0)} / ${Number(manifest.total || 0)} scenes`;
            }

            if (current) {
                current.textContent = manifest.current
                    ? `Current scene: ${manifest.current.scene_id}`
                    : "";
                current.hidden = !manifest.current;
            }

            if (error) {
                error.textContent = manifest.error || "";
                error.hidden = !manifest.error;
            }

            updateVideoSceneCards(panel, projectId, manifest);

            const previousStatus = panel.dataset.videoStatus;
            const nextStatus = manifest.status || previousStatus;

            panel.dataset.videoStatus = nextStatus;

            if (isActive(previousStatus) && !isActive(nextStatus)) {
                await loadProjects();
                return;
            }
        }

        audioPollingEnabled =
            panels.some(panel => isActive(panel.dataset.audioStatus)) ||
            videoPanels.some(panel => isActive(panel.dataset.videoStatus));

    } catch (error) {
        console.error("Production status polling error:", error);
    } finally {
        audioPollInFlight = false;
    }
}, 3000);

initializeKaggleWorkerForm();
setPage("overview");
loadProjects();
