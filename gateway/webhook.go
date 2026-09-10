// Webhook delivery: one queue and one sender per session.
//
// This used to be a single queue drained by four shared workers. That is the
// right shape for one Odoo and the wrong shape for several: an Odoo that is
// down costs four attempts and ~14s of backoff *per event*, so four such events
// occupy every worker and hold up everybody else's messages behind them. One
// client's outage would become an outage for all of them.
//
// A goroutine per session costs a few KB and removes the coupling entirely. It
// also makes delivery ordered within a session, which the shared pool never
// was — four workers racing could hand Odoo a receipt before the message it
// acknowledges.
package main

import (
	"bytes"
	"io"
	"log"
	"net/http"
	"sync"
	"time"
)

var (
	// Events waiting to be posted, per session. A full queue means this Odoo
	// has been unreachable for a long while; dropping is the same loss the
	// retries would end in anyway, without the backpressure.
	webhookQueueSize = envIntOr("WMG_WEBHOOK_QUEUE", 512)
	// Attempts per event before giving up, while the target looks healthy.
	webhookAttempts = 4
	// Consecutive give-ups before we stop spending 14s on every event and
	// switch to one probe at a time.
	webhookBreakerAfter = 3
	webhookProbeEvery   = 30 * time.Second
)

// webhookJob is one event waiting to be posted to Odoo.
type webhookJob struct {
	event string
	body  []byte
}

// webhookClient is shared so senders reuse connections instead of opening a
// fresh one per attempt.
var webhookClient = &http.Client{Timeout: 15 * time.Second}

type webhookSender struct {
	client string // owning Odoo (registry key half)
	name   string // session name as Odoo knows it
	queue  chan webhookJob
	done   chan struct{}
	exited chan struct{}

	mu          sync.Mutex
	stopped     bool
	fails       int // consecutive give-ups
	lastOK      time.Time
	lastErr     string
	lastErrAt   time.Time
	lastAttempt time.Time
	dropped     int
	lastDropLog time.Time
}

func newWebhookSender(client, name string) *webhookSender {
	w := &webhookSender{
		client: client,
		name:   name,
		queue:  make(chan webhookJob, webhookQueueSize),
		done:   make(chan struct{}),
		exited: make(chan struct{}),
	}
	go w.run()
	return w
}

// enqueue must never block: it is called from whatsmeow's event handler, and
// stalling there stalls the WhatsApp connection itself.
func (w *webhookSender) enqueue(job webhookJob) {
	w.mu.Lock()
	stopped := w.stopped
	w.mu.Unlock()
	if stopped {
		return
	}
	select {
	case w.queue <- job:
	default:
		w.noteDrop(job.event)
	}
}

func (w *webhookSender) noteDrop(event string) {
	w.mu.Lock()
	w.dropped++
	n := w.dropped
	quiet := time.Since(w.lastDropLog) < time.Minute
	if !quiet {
		w.lastDropLog = time.Now()
	}
	w.mu.Unlock()
	if !quiet {
		log.Printf("[%s/%s] webhook queue full (%d), dropped %d event(s), latest %s",
			w.client, w.name, cap(w.queue), n, event)
	}
}

func (w *webhookSender) stop() {
	w.mu.Lock()
	if w.stopped {
		w.mu.Unlock()
		return
	}
	w.stopped = true
	w.mu.Unlock()
	close(w.done)
}

// wait blocks until the delivery goroutine has returned, so a caller that is
// about to tear down what deliver reads (the registry, an HTTP test server) is
// not racing a POST that is still in flight.
func (w *webhookSender) wait(timeout time.Duration) {
	select {
	case <-w.exited:
	case <-time.After(timeout):
	}
}

func (w *webhookSender) run() {
	defer close(w.exited)
	for {
		select {
		case <-w.done:
			return
		case job := <-w.queue:
			w.deliver(job)
		}
	}
}

// degraded reports whether this Odoo has been failing long enough that we
// should stop spending the full retry budget on every event.
func (w *webhookSender) degraded() bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.fails >= webhookBreakerAfter
}

func (w *webhookSender) deliver(job webhookJob) {
	// Resolved per event rather than captured at enqueue time, so a client that
	// re-points its webhook is followed on the very next event.
	url, secret := reg.webhookFor(w.client, w.name)
	if url == "" {
		w.noteFailure("no webhook_url registered for this session")
		return
	}

	attempts := webhookAttempts
	if w.degraded() {
		// While the target is down, one probe every webhookProbeEvery and
		// nothing else: a backlog then drains at the speed of the drop rather
		// than 14s per event, and the first success clears the streak.
		w.mu.Lock()
		tooSoon := time.Since(w.lastAttempt) < webhookProbeEvery
		w.mu.Unlock()
		if tooSoon {
			w.noteDrop(job.event)
			return
		}
		attempts = 1
	}

	w.mu.Lock()
	w.lastAttempt = time.Now()
	w.mu.Unlock()

	backoff := 2 * time.Second
	var lastErr string
	for attempt := 1; attempt <= attempts; attempt++ {
		req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(job.body))
		if err != nil {
			w.noteFailure("build error: " + err.Error())
			return
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Webhook-Secret", secret)

		resp, err := webhookClient.Do(req)
		if err == nil {
			// Drain before closing, so the connection can be reused.
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if resp.StatusCode >= 200 && resp.StatusCode < 300 {
				w.noteSuccess()
				return
			}
			lastErr = "HTTP " + resp.Status
			log.Printf("[%s/%s] odoo webhook %s (attempt %d)", w.client, w.name, lastErr, attempt)
		} else {
			lastErr = err.Error()
			log.Printf("[%s/%s] odoo webhook error: %v (attempt %d)", w.client, w.name, err, attempt)
		}
		if attempt < attempts {
			select {
			case <-w.done:
				return
			case <-time.After(backoff):
			}
			backoff *= 2
		}
	}
	log.Printf("[%s/%s] odoo webhook: giving up on event %s", w.client, w.name, job.event)
	w.noteFailure(lastErr)
}

func (w *webhookSender) noteSuccess() {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.fails >= webhookBreakerAfter {
		log.Printf("[%s/%s] odoo webhook recovered after %d failures", w.client, w.name, w.fails)
	}
	w.fails = 0
	w.lastOK = time.Now()
	w.lastErr = ""
}

func (w *webhookSender) noteFailure(msg string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.fails++
	w.lastErr = msg
	w.lastErrAt = time.Now()
	if w.fails == webhookBreakerAfter {
		log.Printf("[%s/%s] odoo webhook failing; backing off to one probe every %s",
			w.client, w.name, webhookProbeEvery)
	}
}

// health is what GET /sessions/{name}/status reports, so Odoo can show an
// operator that its own end is the thing that is broken.
func (w *webhookSender) health() map[string]any {
	w.mu.Lock()
	defer w.mu.Unlock()
	out := map[string]any{
		"queue_depth":     len(w.queue),
		"webhook_failing": w.fails >= webhookBreakerAfter,
		"webhook_dropped": w.dropped,
	}
	if !w.lastOK.IsZero() {
		out["webhook_ok_at"] = w.lastOK.UTC().Format(time.RFC3339)
	}
	if w.lastErr != "" {
		out["webhook_error"] = w.lastErr
		out["webhook_error_at"] = w.lastErrAt.UTC().Format(time.RFC3339)
	}
	return out
}
