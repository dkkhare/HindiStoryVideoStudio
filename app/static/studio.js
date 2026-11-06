
const form = document.getElementById("project-form");
const message = document.getElementById("message");
const list = document.getElementById("projects");

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
        const scenes = status.scene_images;\n        const audio = status.audio;

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
    heading.textContent = "Audio Production";
    panel.appendChild(heading);

    if (audioStage && audioStage.status === "running") {
        setMessage(panel, audioStage.detail || "Generating audio...");
        workflow.appendChild(panel);
        return;
    }

    if (audioStage && audioStage.status === "completed") {
        setMessage(panel, audioStage.detail || "Audio generation completed.");
        try {
            const manifest = await api(`/api/projects/${project.id}/audio`);
            for (const scene of (manifest.scenes || [])) {
                const section = document.createElement("div");
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
                    const link = document.createElement("a");
                    link.href = `/api/projects/${project.id}/assets/${segment.audio}`;
                    link.textContent = `Download ${segment.segment_id} · ${segment.speaker}`;
                    link.style.display = "block";
                    link.target = "_blank";
                    section.appendChild(link);
                }
                panel.appendChild(section);
            }
        } catch (error) {
            setMessage(panel, `Could not load generated audio: ${error.message}`);
        }
        workflow.appendChild(panel);
        return;
    }

    try {
        const profiles = await api("/api/voicebox/profiles");
        const hindiProfiles = profiles.filter(profile => profile.language === "hi");
        const available = hindiProfiles.length ? hindiProfiles : profiles;
        if (!available.length) {
            setMessage(panel, "No Voicebox voice profiles are available.");
            workflow.appendChild(panel);
            return;
        }

        const label = document.createElement("label");
        label.textContent = "Voice profile ";
        const select = document.createElement("select");
        for (const profile of available) {
            const option = document.createElement("option");
            option.value = profile.id;
            option.textContent = `${profile.name} · ${profile.language}${profile.default_engine ? " · " + profile.default_engine : ""}`;
            if (profile.name.toLowerCase() === "niya") option.selected = true;
            select.appendChild(option);
        }
        label.appendChild(select);
        panel.appendChild(label);

        const note = document.createElement("p");
        note.textContent = "Audio can be generated independently of scene images. The selected voice will be used for narration and all character dialogue in this first version. Voice generation runs one segment at a time to limit GPU memory use.";
        panel.appendChild(note);

        panel.appendChild(createButton(
            "Generate Scene Audio",
            async button => {
                button.disabled = true;
                button.textContent = "Generating audio... please wait";
                try {
                    await api(`/api/projects/${project.id}/audio/generate`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            profile_id: select.value,
                            engine: "chatterbox"
                        })
                    });
                    await loadProjects();
                } catch (error) {
                    button.disabled = false;
                    button.textContent = "Generate Scene Audio";
                    setMessage(panel, error.message);
                }
            }
        ));
    } catch (error) {
        setMessage(panel, `Voicebox is not available: ${error.message}`);
    }
    workflow.appendChild(panel);
}
