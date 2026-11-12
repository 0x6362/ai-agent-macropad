;;; agent-shell-macropad-tests.el --- Bridge behavior tests -*- lexical-binding: t; -*-

(require 'ert)
(require 'agent-shell-macropad)

(defmacro agent-shell-macropad-test--session (&rest body)
  "Run BODY with an isolated session and captured outgoing events."
  (declare (indent 0))
  `(with-temp-buffer
     (setq-local major-mode 'agent-shell-mode)
     (let ((agent-shell--state `((:buffer . ,(current-buffer))
                                (:session . ((:id . "one")))
                                (:event-subscriptions . nil)))
           (agent-shell-macropad--session-id nil)
           (agent-shell-macropad--subscription nil)
           (agent-shell-macropad--tool-statuses nil)
           (events nil))
       (cl-letf (((symbol-function 'agent-shell-macropad--send)
                  (lambda (event &optional tool)
                    (push (list event tool) events) t))
                 ((symbol-function 'agent-shell-status) (lambda (&rest _) 'ready)))
         ,@body))))

(ert-deftest agent-shell-macropad-session-registers-once-and-cleans-up ()
  (agent-shell-macropad-test--session
    (agent-shell-macropad--handle '((:event . input-submitted)))
    (agent-shell-macropad--handle '((:event . turn-complete)))
    (agent-shell-macropad--handle '((:event . clean-up)))
    (should (equal (mapcar #'car (reverse events))
                   '("SessionStart" "UserPromptSubmit" "Stop" "SessionEnd")))
    (should-not agent-shell-macropad--session-id)))

(ert-deftest agent-shell-macropad-session-switch-retires-the-old-identity ()
  (agent-shell-macropad-test--session
    (agent-shell-macropad--sync-session)
    (map-put! (map-elt agent-shell--state :session) :id "two")
    (agent-shell-macropad--sync-session)
    (should (equal agent-shell-macropad--session-id "agent-shell:two"))
    (should (equal (mapcar #'car (reverse events))
                   '("SessionStart" "SessionEnd" "SessionStart")))))

(ert-deftest agent-shell-macropad-resync-retains-identity-and-live-status ()
  (dolist (status '(ready busy blocked))
    (agent-shell-macropad-test--session
      (let ((buffer (current-buffer))
            (agent-shell-macropad-mode t))
        (setq agent-shell-macropad--session-id "agent-shell:one")
        (cl-letf (((symbol-function 'agent-shell-buffers) (lambda () (list buffer)))
                  ((symbol-function 'agent-shell-status) (lambda (&rest _) status)))
          (agent-shell-macropad-resync))
        (should (equal agent-shell-macropad--session-id "agent-shell:one"))
        (should (equal events
                       (list (list (pcase status
                                     ('ready "Notification")
                                     ('busy "UserPromptSubmit")
                                     ('blocked "PermissionRequest")) nil))))))))

(ert-deftest agent-shell-macropad-session-snapshot-is-read-only ()
  (with-temp-buffer
    (setq-local agent-shell-macropad--session-id "agent-shell:one")
    (let ((buffer (current-buffer)))
      (cl-letf (((symbol-function 'agent-shell-buffers) (lambda () (list buffer)))
                ((symbol-function 'agent-shell-macropad--send)
                 (lambda (&rest _) (ert-fail "Snapshot must not emit state changes"))))
        (should (equal (agent-shell-macropad-session-ids) ["agent-shell:one"]))
        (should (eq buffer (current-buffer)))))))

(ert-deftest agent-shell-macropad-empty-snapshot-is-a-vector ()
  (cl-letf (((symbol-function 'agent-shell-buffers) (lambda () nil)))
    (should (equal (json-encode (agent-shell-macropad-session-ids)) "[]"))))

(ert-deftest agent-shell-macropad-tool-updates-deduplicate-and-keep-other-tools-active ()
  (agent-shell-macropad-test--session
    (dolist (data '(((:tool-call-id . "a") (:tool-call . ((:status . "in_progress"))))
                    ((:tool-call-id . "a") (:tool-call . ((:status . "in_progress"))))
                    ((:tool-call-id . "b") (:tool-call . ((:status . "in_progress"))))
                    ((:tool-call-id . "a") (:tool-call . ((:status . "completed"))))
                    ((:tool-call-id . "b") (:tool-call . ((:status . "completed"))))))
      (agent-shell-macropad--handle `((:event . tool-call-update) (:data . ,data))))
    (should (equal (mapcar #'car (reverse events))
                   '("SessionStart" "PreToolUse" "PreToolUse" "PreToolUse" "PostToolUse")))))

(ert-deftest agent-shell-macropad-permission-state-survives-tool-updates ()
  (agent-shell-macropad-test--session
    (agent-shell-macropad--handle '((:event . permission-request)))
    (cl-letf (((symbol-function 'agent-shell-status) (lambda (&rest _) 'blocked)))
      (agent-shell-macropad--handle
       '((:event . tool-call-update)
         (:data . ((:tool-call-id . "a") (:tool-call . ((:status . "pending"))))))))
    (should (equal (caar events) "PermissionRequest"))))

(ert-deftest agent-shell-macropad-does-not-forward-prompts-or-message-chunks ()
  (agent-shell-macropad-test--session
    (agent-shell-macropad--handle '((:event . agent-message-chunk)
                                   (:data . ((:text-chunk . "private content")))))
    (should (equal events '(("SessionStart" nil))))))

(ert-deftest agent-shell-macropad-child-environment-preserves-input ()
  (let* ((arguments '(:command "agent" :environment-variables ("EXAMPLE=value")))
         (before (copy-tree arguments))
         (result (agent-shell-macropad--client-arguments arguments)))
    (should (equal arguments before))
    (should (equal (plist-get result :environment-variables)
                   '("AI_AGENT_MACROPAD_SOURCE=agent-shell" "EXAMPLE=value")))))

(ert-deftest agent-shell-macropad-idle-permission-remains-needs-input ()
  (should (equal (agent-shell-macropad--event 'idle '((:idle-event . permission-request)))
                 "PermissionRequest"))
  (should (equal (agent-shell-macropad--event 'idle '((:idle-event . turn-complete)))
                 "Notification")))

(ert-deftest agent-shell-macropad-focus-uses-identity-after-buffer-rename ()
  (with-temp-buffer
    (setq-local agent-shell-macropad--session-id "agent-shell:one")
    (let ((target (current-buffer)) focused)
      (rename-buffer " *renamed-macropad-test*" t)
      (cl-letf (((symbol-function 'agent-shell-buffers) (lambda () (list target)))
                ((symbol-function 'pop-to-buffer) (lambda (buffer &rest _) (setq focused buffer)))
                ((symbol-function 'select-frame-set-input-focus) #'ignore))
        (should (agent-shell-macropad-focus-session "agent-shell:one" t))
        (should-not focused)
        (should (agent-shell-macropad-focus-session "agent-shell:one"))
        (should (eq target focused))
        (should-not (agent-shell-macropad-focus-session "another-session"))))))

(defun agent-shell-macropad-test--permission (id)
  `((:permission-request-id . ,id)
    (:permission-actions .
     (((:kind . "allow_always") (:option-id . "always"))
      ((:kind . "allow_once") (:option-id . "once"))
      ((:kind . "reject_once") (:option-id . "reject"))))))

(ert-deftest agent-shell-macropad-approval-is-exact-and-never-allows-always ()
  (with-temp-buffer
    (setq-local agent-shell-macropad--session-id "agent-shell:one")
    (let* ((target (current-buffer))
           (agent-shell--state
            (list (cons :buffer target)
                  (cons :client 'fake-client)
                  (cons :tool-calls
                        (list (cons "tool" (agent-shell-macropad-test--permission 42))))))
           response)
      (cl-letf (((symbol-function 'agent-shell-buffers) (lambda () (list target)))
                ((symbol-function 'agent-shell--send-permission-response)
                 (lambda (&rest arguments) (setq response arguments))))
        (should-not (agent-shell-macropad-respond "agent-shell:one" 41 "allow"))
        (should-not response)
        (should (agent-shell-macropad-respond "agent-shell:one" 42 "allow"))
        (should (equal (plist-get response :option-id) "once"))
        (should (agent-shell-macropad-respond "agent-shell:one" 42 "deny"))
        (should (equal (plist-get response :option-id) "reject"))
        (setf (alist-get :permission-actions
                         (cdar (map-elt agent-shell--state :tool-calls)))
              '(((:kind . "allow_always") (:option-id . "always"))))
        (setq response nil)
        (should-not (agent-shell-macropad-respond "agent-shell:one" 42 "allow"))
        (should-not response)
        (should (agent-shell-macropad-respond "agent-shell:one" 42 "deny"))
        (should (plist-get response :cancelled))))))

(ert-deftest agent-shell-macropad-multiple-pending-requests-are-ambiguous ()
  (with-temp-buffer
    (setq-local agent-shell-macropad--session-id "agent-shell:one")
    (let ((target (current-buffer))
          (agent-shell--state
           (list (cons :tool-calls
                       (list (cons "a" (agent-shell-macropad-test--permission 1))
                             (cons "b" (agent-shell-macropad-test--permission 2)))))))
      (cl-letf (((symbol-function 'agent-shell-buffers) (lambda () (list target)))
                ((symbol-function 'agent-shell--send-permission-response)
                 (lambda (&rest _) (ert-fail "Ambiguous request must not be answered"))))
        (should-not (agent-shell-macropad-respond "agent-shell:one" 2 "allow"))))))

(ert-deftest agent-shell-macropad-public-subscription-receives-lifecycle-events ()
  (agent-shell-macropad-test--session
    (cl-letf (((symbol-function 'agent-shell--sync-system-sleep) #'ignore))
      (agent-shell-macropad--attach)
      (agent-shell--emit-event :event 'input-submitted)
      (agent-shell--emit-event :event 'turn-complete)
      (should (equal (mapcar #'car (reverse events))
                     '("SessionStart" "UserPromptSubmit" "Stop")))
      (agent-shell-unsubscribe :subscription agent-shell-macropad--subscription))))

(provide 'agent-shell-macropad-tests)
