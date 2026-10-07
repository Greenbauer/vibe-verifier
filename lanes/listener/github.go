package main

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// installationRepo is the part of a repository the user scope filters on.
type installationRepo struct {
	Name     string `json:"name"`
	Private  bool   `json:"private"`
	Archived bool   `json:"archived"`
	Owner    struct {
		Login string `json:"login"`
	} `json:"owner"`
}

// appClient lists the App installation's repositories with an installation token it mints from
// the App key. Neither the key, the JWT nor the token is ever part of an error or a log line.
type appClient struct {
	api            string // https://api.github.com; a test server in tests
	http           *http.Client
	clientID       string
	installationID int64
	key            *rsa.PrivateKey
	now            func() time.Time
}

const (
	reposPerPage = 100
	reposMaxPage = 50 // 5000 repositories: a bound, so a misbehaving API cannot page forever
)

func newAppClient(clientID string, installationID int64, pemKey []byte) (*appClient, error) {
	key, err := parsePrivateKey(pemKey)
	if err != nil {
		return nil, err
	}
	return &appClient{
		api: "https://api.github.com", http: &http.Client{Timeout: 30 * time.Second},
		clientID: clientID, installationID: installationID, key: key, now: time.Now,
	}, nil
}

// parsePrivateKey reads the App key GitHub issues (PKCS#1) or its PKCS#8 form.
func parsePrivateKey(pemKey []byte) (*rsa.PrivateKey, error) {
	block, _ := pem.Decode(pemKey)
	if block == nil {
		return nil, errors.New("the App key file holds no PEM block")
	}
	if key, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return key, nil
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, errors.New("the App key is neither a PKCS#1 nor a PKCS#8 private key")
	}
	key, ok := parsed.(*rsa.PrivateKey)
	if !ok {
		return nil, errors.New("the App key is not an RSA key")
	}
	return key, nil
}

// appJWT is the RS256 token an App authenticates with: iss is the App's client id, iat is
// backdated a minute for clock drift, and exp stays inside GitHub's ten-minute limit.
func (a *appClient) appJWT() (string, error) {
	now := a.now()
	claims, err := json.Marshal(map[string]any{
		"iat": now.Add(-time.Minute).Unix(), "exp": now.Add(9 * time.Minute).Unix(), "iss": a.clientID,
	})
	if err != nil {
		return "", err
	}
	enc := base64.RawURLEncoding
	signed := enc.EncodeToString([]byte(`{"alg":"RS256","typ":"JWT"}`)) + "." + enc.EncodeToString(claims)
	digest := sha256.Sum256([]byte(signed))
	sig, err := rsa.SignPKCS1v15(rand.Reader, a.key, crypto.SHA256, digest[:])
	if err != nil {
		return "", errors.New("signing the App JWT failed")
	}
	return signed + "." + enc.EncodeToString(sig), nil
}

func (a *appClient) installationToken(ctx context.Context) (string, error) {
	jwt, err := a.appJWT()
	if err != nil {
		return "", err
	}
	var out struct {
		Token string `json:"token"`
	}
	path := fmt.Sprintf("/app/installations/%d/access_tokens", a.installationID)
	if err := a.call(ctx, http.MethodPost, path, jwt, http.StatusCreated, &out); err != nil {
		return "", err
	}
	if out.Token == "" {
		return "", fmt.Errorf("POST %s: the response carried no token", path)
	}
	return out.Token, nil
}

// repositories lists every repository of the App installation, page by page.
func (a *appClient) repositories(ctx context.Context) ([]installationRepo, error) {
	token, err := a.installationToken(ctx)
	if err != nil {
		return nil, err
	}
	var all []installationRepo
	for page := 1; page <= reposMaxPage; page++ {
		var out struct {
			TotalCount   int                `json:"total_count"`
			Repositories []installationRepo `json:"repositories"`
		}
		path := fmt.Sprintf("/installation/repositories?per_page=%d&page=%d", reposPerPage, page)
		if err := a.call(ctx, http.MethodGet, path, token, http.StatusOK, &out); err != nil {
			return nil, err
		}
		all = append(all, out.Repositories...)
		if len(out.Repositories) < reposPerPage || len(all) >= out.TotalCount {
			return all, nil
		}
	}
	return nil, fmt.Errorf("the installation lists more than %d repositories", reposPerPage*reposMaxPage)
}

func (a *appClient) call(ctx context.Context, method, path, bearer string, want int, out any) error {
	req, err := http.NewRequestWithContext(ctx, method, a.api+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	resp, err := a.http.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	if resp.StatusCode != want {
		snippet := strings.TrimSpace(string(body))
		if len(snippet) > 200 {
			snippet = snippet[:200]
		}
		return fmt.Errorf("%s %s: HTTP %d: %s", method, path, resp.StatusCode, snippet)
	}
	if err := json.Unmarshal(body, out); err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	return nil
}

// servedTargets splits the owner's repositories into the ones the lane serves (private, not
// archived, not excluded) and the rest, both as owner/name targets. Public repositories are never
// served: a fork's pull request must not reach a self-hosted runner.
func servedTargets(repos []installationRepo, owner string, exclude []string) (served, unserved []string) {
	excluded := map[string]bool{}
	for _, name := range exclude {
		excluded[strings.ToLower(name)] = true
	}
	for _, r := range repos {
		if !strings.EqualFold(r.Owner.Login, owner) {
			continue
		}
		target := owner + "/" + r.Name
		if r.Private && !r.Archived && !excluded[strings.ToLower(r.Name)] {
			served = append(served, target)
		} else {
			unserved = append(unserved, target)
		}
	}
	return served, unserved
}
