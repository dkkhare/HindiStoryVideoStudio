
const form = document.getElementById("project-form");
const message = document.getElementById("message");
const list = document.getElementById("projects");
let audioPollingEnabled = false;

async function api(url, options = {}) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
        throw new Error(data.detail || "Request failed");
    }

    return data;
}

function createButton(label, handler) {
    const button = document.createElement("button");
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

async function loadProjects() {
    audioPollingEnabled = false;
    list.textContent = "Loading projects...";

    try {
        const projects = await api("/api/projects");
        list.innerHTML = "";

        if (!projects.length) {
            list.textContent = "No projects yet. Create your first story above.";
            return;
        }

        for (const project of projects) {
            await renderProject(project);
        }
    } catch (error) {
        list.textContent = error.message;
    }
}

async function renderProject(project) {
    const card = document.createElement("div");
    card.className = "project";

    const heading = document.createElement("h3");
    heading.textContent = project.title;

    const details = document.createElement("p");
    details.textContent =
        `${project.target_duration_minutes} minutes · ${project.status}`;

    const workflow = document.createElement("div");
    workflow.className = "workflow";

    card.append(heading, details, workflow);
    list.appendChild(card);

    try {
        const stages = await api(`/api/projects/${project.id}/stages`);
        const status = Object.fromEntries(
            stages.map(stage => [stage.stage_key, stage])
        );

        const screenplay = status.screenplay;
        const references = status.character_bible;
        const scenes = status.scene_images;
        const audio = status.audio;

        if (!screenplay || screenplay.status === "pending" ||
            screenplay.status === "failed") {

            workflow.appendChild(createButton(
                "Generate Screenplay",
                button => runAction(
                    button,
                    project.id,
                    "screenplay/generate",
                    "Generating screenplay..."
                )
            ));

            return;
        }

        if (screenplay.status === "running") {
            setMessage(workflow, "Screenplay is being generated...");
            return;
        }

        if (screenplay.status === "waiting_approval") {
            setMessage(workflow, "Next step: Review and approve your screenplay.");

            const reviewButton = createButton(
                "Review Screenplay",
                async button => {
                    button.disabled = true;

                    try {
                        const data = await api(
                            `/api/projects/${project.id}/screenplay`
                        );

                        const pre = document.createElement("pre");
                        pre.className = "screenplay-preview";
                        pre.textContent = JSON.stringify(
                            data.screenplay,
                            null,
                            2
                        );

                        const existing = workflow.querySelector(
                            ".screenplay-preview"
                        );

                        if (existing) {
                            existing.remove();
                            button.textContent = "Review Screenplay";
                        } else {
                            workflow.appendChild(pre);
                            button.textContent = "Hide Screenplay";
                        }
                    } catch (error) {
                        setMessage(workflow, error.message);
                    } finally {
                        button.disabled = false;
                    }
                }
            );

            workflow.appendChild(reviewButton);

            workflow.appendChild(createButton(
                "Approve Screenplay",
                button => runAction(
                    button,
                    project.id,
                    "screenplay/approve",
                    "Approving screenplay..."
                )
            ));

            return;
        }

        if (screenplay.status === "completed") {

            await renderAudioStage(workflow, project, audio);

            if (!references || references.status === "pending" ||
                references.status === "failed") {

                setMessage(
                    workflow,
                    "Screenplay approved. Next: Generate visual references."
                );

                workflow.appendChild(createButton(
                    "Generate Character, Prop & Location References",
                    button => runAction(
                        button,
                        project.id,
                        "references/generate",
                        "Generating visual references..."
                    )
                ));

                return;
            }

            if (references.status === "running") {
                setMessage(
                    workflow,
                    "Generating character, prop and location references..."
                );
                return;
            }

            if (references.status === "completed") {

                if (!scenes || scenes.status === "pending" ||
                    scenes.status === "failed") {

                    setMessage(
                        workflow,
                        "Visual references ready. Next: Generate scene images."
                    );

                    workflow.appendChild(createButton(
                        "Generate Scene Images",
                        button => runAction(
                            button,
                            project.id,
                            "images/generate",
                            "Generating scene images..."
                        )
                    ));

                    return;
                }

                if (scenes.status === "running") {
                    setMessage(workflow, "Generating scene images...");
                    return;
                }

                if (scenes.status === "completed") {
                    setMessage(
                        workflow,
                        "Scene images completed successfully."
                    );

                    return;
                }
            }
        }

        setMessage(workflow, "Please check the production stage status.");

    } catch (error) {
        setMessage(workflow, `Could not load workflow: ${error.message}`);
    }
}

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

        const card = button.closest(".project");
        if (card) {
            setMessage(card, error.message);
        }
    }
}

form.addEventListener("submit", async event => {
    event.preventDefault();
    message.textContent = "Creating project...";

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

        message.textContent = `Created project #${data.id}`;
        form.reset();
        await loadProjects();

    } catch (error) {
        message.textContent = error.message;
    }
});

loadProjects();



async function renderAudioStage(workflow, project, audioStage) {
    const panel = document.createElement("div");
    panel.className = "audio-stage";
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
        manifest = await api(`/api/projects/${project.id}/audio`).catch(() => null);
    } catch (error) {
        setMessage(panel, `Audio setup unavailable: ${error.message}`);
        workflow.appendChild(panel);
        return;
    }

    const screenplay = screenplayData.screenplay || {};
    const characters = screenplay.characters || [];
    const speakers = characters.map(character => ({
        id: character.character_id,
        name: character.name || character.character_id,
        kind: "character"
    })).filter(item => item.id);
    speakers.push({ id: "NARRATOR", name: "Narrator / Voice-over", kind: "narrator" });

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
        (manifest?.voice_assignments || []).map(item => [item.speaker_id, item.profile_id])
    );
    const selectors = {};
    const assignmentSection = document.createElement("div");
    assignmentSection.className = "voice-assignment-list";
    const assignmentHeading = document.createElement("p");
    assignmentHeading.textContent = "Choose a separate voice profile for every character and for narration:";
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
            option.textContent = `${profile.name} · ${profile.language || "language not specified"}${profile.default_engine ? " · " + profile.default_engine : ""}`;
            if (String(profile.id) === String(savedAssignments[speaker.id] || "")) {
                option.selected = true;
            } else if (!savedAssignments[speaker.id] && speaker.kind === "narrator" &&
                       String(profile.name).toLowerCase() === "niya") {
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
    if (["running", "pause_requested", "stop_requested"].includes(status)) audioPollingEnabled = true;
    const completed = Number(manifest?.completed || 0);
    const total = Number(manifest?.total || manifest?.segments?.length || 0);
    const detail = document.createElement("p");
    detail.className = "message";
    detail.textContent = manifest?.detail || audioStage?.detail ||
        "Assign the voices, then generate the audio.";
    panel.appendChild(detail);

    if (total > 0) {
        const progress = document.createElement("p");
        progress.textContent = `Progress: ${completed} / ${total} segments`;
        panel.appendChild(progress);
    }
    if (manifest?.current) {
        const current = document.createElement("p");
        current.textContent = `Current operation: ${manifest.current.scene_id} · ${manifest.current.segment_id} · ${manifest.current.speaker}`;
        panel.appendChild(current);
    }
    if (manifest?.error) setMessage(panel, manifest.error);

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
                audio.src = `/api/projects/${project.id}/assets/${scene.audio}`;
                audio.style.display = "block";
                audio.style.width = "100%";
                section.appendChild(audio);
            }
            for (const segment of (scene.segments || [])) {
                if (!segment.audio) continue;
                const link = document.createElement("a");
                link.href = `/api/projects/${project.id}/assets/${segment.audio}`;
                link.textContent = `Download ${segment.segment_id} · ${segment.speaker}`;
                link.target = "_blank";
                link.style.display = "block";
                section.appendChild(link);
            }
            panel.appendChild(section);
        }
        if (manifest?.segments?.length) {
            const completedSegments = manifest.segments.filter(item => item.status === "completed");
            if (completedSegments.length && !scenes.length) {
                const listTitle = document.createElement("p");
                listTitle.textContent = "Completed audio segments:";
                panel.appendChild(listTitle);
                for (const segment of completedSegments) {
                    const link = document.createElement("a");
                    link.href = `/api/projects/${project.id}/assets/${segment.audio}`;
                    link.textContent = `Download ${segment.segment_id} · ${segment.speaker}`;
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
                await api(`/api/projects/${project.id}/audio/${endpoint}`, { method: "POST" });
                await loadProjects();
            } catch (error) {
                btn.disabled = false;
                setMessage(panel, error.message);
            }
        });
        if (cssClass) button.className = cssClass;
        controls.appendChild(button);
    };

    if (["running", "pause_requested", "stop_requested"].includes(status)) {
        assignmentSection.querySelectorAll("select").forEach(select => select.disabled = true);
        addControl(status === "running" ? "Pause after current segment" : "Pause requested…", "control/pause", "secondary");
        addControl(status === "running" ? "Stop after current segment" : "Stop requested…", "control/stop", "danger");
    } else {
        const generateButton = createButton(
            manifest ? "Generate / Continue Audio" : "Generate Scene Audio",
            async button => {
                const voiceAssignments = {};
                for (const [speakerId, select] of Object.entries(selectors)) {
                    if (!select.value) {
                        setMessage(panel, `Please select a voice profile for ${speakerId}.`);
                        select.focus();
                        return;
                    }
                    voiceAssignments[speakerId] = select.value;
                }
                button.disabled = true;
                button.textContent = "Starting audio generation…";
                try {
                    await api(`/api/projects/${project.id}/audio/generate`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ voice_assignments: voiceAssignments, engine: "chatterbox" })
                    });
                    await loadProjects();
                } catch (error) {
                    button.disabled = false;
                    button.textContent = manifest ? "Generate / Continue Audio" : "Generate Scene Audio";
                    setMessage(panel, error.message);
                }
            }
        );
        controls.appendChild(generateButton);

        if (manifest && ["paused", "stopped", "failed", "running", "pause_requested", "stop_requested"].includes(status)) {
            addControl("Resume from checkpoint", "resume", "secondary");
        }
    }

    if (status === "completed" || (manifest?.segments || []).some(item => item.status === "completed")) {
        showGeneratedAssets();
    }
    workflow.appendChild(panel);
}


// Refresh stage status while background audio generation is running.
setInterval(() => {
    if (audioPollingEnabled && document.visibilityState === "visible") loadProjects();
}, 3000);
