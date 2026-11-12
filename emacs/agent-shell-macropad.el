;;; agent-shell-macropad.el --- Agent Shell session status and focus -*- lexical-binding: t; -*-

(require 'cl-lib)
(require 'json)
(require 'map)
(require 'seq)
(require 'agent-shell)

(defgroup agent-shell-macropad nil
  "Mirror Agent Shell sessions on an AI agent macropad."
  :group 'agent-shell)

(defcustom agent-shell-macropad-socket
  (expand-file-name "~/.ai-agent-macropad/daemon.sock")
  "Unix socket served by the macropad daemon."
  :type 'file)

(defvar-local agent-shell-macropad--session-id nil)
(defvar-local agent-shell-macropad--subscription nil)
(defvar-local agent-shell-macropad--tool-statuses nil)

(defun agent-shell-macropad--event (event data)
  "Translate EVENT and DATA into an existing daemon hook event."
  (pcase event
    ('input-submitted "UserPromptSubmit")
    ((or 'permission-request 'elicitation-request) "PermissionRequest")
    ((or 'permission-response 'elicitation-response) "UserPromptSubmit")
    ('turn-complete "Stop")
    ('error "StopFailure")
    ('idle (if (memq (map-elt data :idle-event) '(permission-request elicitation-request))
               "PermissionRequest" "Notification"))
    ('tool-call-update
     (pcase (map-nested-elt data '(:tool-call :status))
       ((or "pending" "in_progress") "PreToolUse")
       ("completed" "PostToolUse")
       ("failed" "PostToolUseFailure")))))

(defun agent-shell-macropad--send (event &optional tool)
  "Send EVENT with optional TOOL kind to the daemon.
Socket failures leave the agent session running normally."
  (condition-case nil
      (let* ((process (make-network-process
                       :name "agent-shell-macropad" :family 'local
                       :service agent-shell-macropad-socket
                       :coding 'utf-8-unix :noquery t
                       :sentinel #'ignore :filter #'ignore))
             (payload `((hook_event_name . ,event)
                        (session_id . ,agent-shell-macropad--session-id)
                        (emacs_session . ,agent-shell-macropad--session-id)
                        (agent . "agent-shell")
                        (cwd . ,(expand-file-name default-directory)))))
        (when tool
          (push (cons 'tool_name tool) payload))
        (let ((pending (seq-filter
                        (lambda (entry) (map-elt (cdr entry) :permission-request-id))
                        (map-elt agent-shell--state :tool-calls))))
          (push (cons 'permission_count (length pending)) payload)
          (when (= (length pending) 1)
            (push (cons 'permission_request_id
                        (map-elt (cdar pending) :permission-request-id)) payload)))
        (when (equal event "Notification")
          (push '(notification_type . "idle_prompt") payload))
        (unwind-protect
            (process-send-string process (concat (json-encode payload) "\n"))
          (delete-process process))
        t)
    (error nil)))

(defun agent-shell-macropad--sync-session ()
  "Register the current ACP session, retiring any previous session."
  (when-let* ((id (map-nested-elt agent-shell--state '(:session :id))))
    (let ((identity (concat "agent-shell:" id)))
      (unless (equal identity agent-shell-macropad--session-id)
        (when agent-shell-macropad--session-id
          (agent-shell-macropad--send "SessionEnd"))
        (setq agent-shell-macropad--session-id identity
              agent-shell-macropad--tool-statuses nil)
        (unless (agent-shell-macropad--send "SessionStart")
          (setq agent-shell-macropad--session-id nil)))))
  agent-shell-macropad--session-id)

(defun agent-shell-macropad--handle (notification)
  "Forward a lifecycle NOTIFICATION from Agent Shell."
  (let ((event (map-elt notification :event))
        (data (map-elt notification :data)))
    (if (eq event 'clean-up)
        (when agent-shell-macropad--session-id
          (agent-shell-macropad--send "SessionEnd")
          (setq agent-shell-macropad--session-id nil))
      (when (agent-shell-macropad--sync-session)
        (when-let* ((hook-event (agent-shell-macropad--event event data)))
          (if (eq event 'tool-call-update)
              (let* ((id (map-elt data :tool-call-id))
                     (status (map-nested-elt data '(:tool-call :status)))
                     (kind (or (map-nested-elt data '(:tool-call :kind)) "tool")))
                (unless (equal status (alist-get id agent-shell-macropad--tool-statuses nil nil #'equal))
                  (setf (alist-get id agent-shell-macropad--tool-statuses nil nil #'equal) status)
                  (when (and (equal hook-event "PostToolUse")
                             (seq-some (lambda (entry)
                                         (member (cdr entry) '("pending" "in_progress")))
                                       agent-shell-macropad--tool-statuses))
                    (setq hook-event "PreToolUse"))
                  (when (eq (agent-shell-status) 'blocked)
                    (setq hook-event "PermissionRequest"))
                  (agent-shell-macropad--send hook-event kind)))
            (when (eq event 'input-submitted)
              (setq agent-shell-macropad--tool-statuses nil))
            (agent-shell-macropad--send hook-event)))))))

(defun agent-shell-macropad--attach ()
  "Subscribe the current Agent Shell buffer to macropad updates."
  (unless agent-shell-macropad--subscription
    (setq agent-shell-macropad--subscription
          (agent-shell-subscribe-to :shell-buffer (current-buffer)
                                    :on-event #'agent-shell-macropad--handle))
    (when (agent-shell-macropad--sync-session)
      (pcase (agent-shell-status)
        ('blocked (agent-shell-macropad--send "PermissionRequest"))
        ('busy (agent-shell-macropad--send "UserPromptSubmit"))))))

(defun agent-shell-macropad-resync ()
  "Reannounce open sessions and their current status after daemon restart."
  (when agent-shell-macropad-mode
    (dolist (buffer (agent-shell-buffers))
      (with-current-buffer buffer
        (when (agent-shell-macropad--sync-session)
          (agent-shell-macropad--send
           (pcase (agent-shell-status)
             ('blocked "PermissionRequest")
             ('busy "UserPromptSubmit")
             (_ "Notification")))))))
  t)

(defun agent-shell-macropad--client-arguments (arguments)
  "Mark ACP child process ARGUMENTS so native CLI hooks skip them."
  (let ((copy (copy-sequence arguments)))
    (plist-put copy :environment-variables
               (cons "AI_AGENT_MACROPAD_SOURCE=agent-shell"
                     (seq-remove (lambda (value)
                                   (string-prefix-p "AI_AGENT_MACROPAD_SOURCE=" value))
                                 (plist-get arguments :environment-variables))))))

(defun agent-shell-macropad-focus-session (session-id &optional probe-only)
  "Focus the shell identified by SESSION-ID.
With PROBE-ONLY, report its existence without changing focus."
  (when-let* ((buffer (seq-find
                      (lambda (buffer)
                        (equal (buffer-local-value 'agent-shell-macropad--session-id buffer)
                               session-id))
                      (agent-shell-buffers))))
    (unless probe-only
      (pop-to-buffer buffer)
      (select-frame-set-input-focus (selected-frame)))
    t))

(defun agent-shell-macropad-respond (session-id request-id decision)
  "Answer exactly REQUEST-ID in SESSION-ID with DECISION.
Only a single pending tool request is eligible.  Allow chooses
`allow_once'; deny chooses `reject_once' or cancels the request."
  (when-let* ((buffer (seq-find
                      (lambda (buffer)
                        (equal (buffer-local-value 'agent-shell-macropad--session-id buffer)
                               session-id))
                      (agent-shell-buffers))))
    (with-current-buffer buffer
      (let ((pending (seq-filter
                      (lambda (entry) (map-elt (cdr entry) :permission-request-id))
                      (map-elt agent-shell--state :tool-calls))))
        (when (and (= (length pending) 1)
                   (equal request-id (map-elt (cdar pending) :permission-request-id))
                   (member decision '("allow" "deny")))
          (let* ((tool (car pending))
                 (kind (if (equal decision "allow") "allow_once" "reject_once"))
                 (option (seq-find
                          (lambda (action) (equal (map-elt action :kind) kind))
                          (map-elt (cdr tool) :permission-actions))))
            (when (or option (equal decision "deny"))
              (agent-shell--send-permission-response
               :client (map-elt agent-shell--state :client)
               :request-id request-id
               :option-id (when option (map-elt option :option-id))
               :cancelled (not option)
               :state agent-shell--state
               :tool-call-id (car tool))
              t)))))))

;;;###autoload
(define-minor-mode agent-shell-macropad-mode
  "Mirror Agent Shell sessions on the macropad."
  :global t :group 'agent-shell-macropad
  (if agent-shell-macropad-mode
      (progn
        (add-hook 'agent-shell-mode-hook #'agent-shell-macropad--attach)
        (advice-add 'agent-shell--make-acp-client :filter-args
                    #'agent-shell-macropad--client-arguments)
        (dolist (buffer (agent-shell-buffers))
          (with-current-buffer buffer (agent-shell-macropad--attach))))
    (remove-hook 'agent-shell-mode-hook #'agent-shell-macropad--attach)
    (advice-remove 'agent-shell--make-acp-client
                   #'agent-shell-macropad--client-arguments)
    (dolist (buffer (agent-shell-buffers))
      (with-current-buffer buffer
        (when agent-shell-macropad--session-id
          (agent-shell-macropad--send "SessionEnd"))
        (when agent-shell-macropad--subscription
          (agent-shell-unsubscribe :subscription agent-shell-macropad--subscription))
        (setq agent-shell-macropad--subscription nil
              agent-shell-macropad--session-id nil
              agent-shell-macropad--tool-statuses nil)))))

(provide 'agent-shell-macropad)
;;; agent-shell-macropad.el ends here
