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
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

var (
	testKeyOnce sync.Once
	testKey     *rsa.PrivateKey
)

func appKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	testKeyOnce.Do(func() {
		var err error
		if testKey, err = rsa.GenerateKey(rand.Reader, 2048); err != nil {
			panic(err)
		}
	})
	return testKey
}

// fakeGitHub serves the two endpoints the user scope uses, checking the credentials each needs.
type fakeGitHub struct {
	t        *testing.T
	key      *rsa.PublicKey
	now      time.Time
	repos    []installationRepo
	tokens   int
	pages    []int
	tokenErr int // HTTP status for the token exchange; 0 means 201
}

func (f *fakeGitHub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodPost && r.URL.Path == "/app/installations/77/access_tokens":
		f.checkJWT(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
		if f.tokenErr != 0 {
			w.WriteHeader(f.tokenErr)
			fmt.Fprint(w, `{"message":"Bad credentials"}`)
			return
		}
		f.tokens++
		w.WriteHeader(http.StatusCreated)
		fmt.Fprint(w, `{"token":"ghs_installation","expires_at":"2026-01-01T13:00:00Z"}`)
	case r.Method == http.MethodGet && r.URL.Path == "/installation/repositories":
		if r.Header.Get("Authorization") != "Bearer ghs_installation" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		page, _ := strconv.Atoi(r.URL.Query().Get("page"))
		per, _ := strconv.Atoi(r.URL.Query().Get("per_page"))
		f.pages = append(f.pages, page)
		lo, hi := min((page-1)*per, len(f.repos)), min(page*per, len(f.repos))
		_ = json.NewEncoder(w).Encode(map[string]any{"total_count": len(f.repos), "repositories": f.repos[lo:hi]})
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

func (f *fakeGitHub) checkJWT(jwt string) {
	parts := strings.Split(jwt, ".")
	if len(parts) != 3 {
		f.t.Errorf("JWT has %d parts", len(parts))
		return
	}
	sig, _ := base64.RawURLEncoding.DecodeString(parts[2])
	digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if err := rsa.VerifyPKCS1v15(f.key, crypto.SHA256, digest[:], sig); err != nil {
		f.t.Errorf("JWT signature: %v", err)
	}
	var header map[string]string
	raw, _ := base64.RawURLEncoding.DecodeString(parts[0])
	_ = json.Unmarshal(raw, &header)
	if header["alg"] != "RS256" {
		f.t.Errorf("JWT header %v", header)
	}
	var claims struct {
		Iat, Exp int64
		Iss      string
	}
	raw, _ = base64.RawURLEncoding.DecodeString(parts[1])
	_ = json.Unmarshal(raw, &claims)
	if claims.Iss != "123456" || claims.Iat > f.now.Unix() || claims.Exp <= f.now.Unix() || claims.Exp-claims.Iat > 600 {
		f.t.Errorf("JWT claims %+v at %d", claims, f.now.Unix())
	}
}

func testApp(t *testing.T, repos []installationRepo) (*appClient, *fakeGitHub) {
	key := appKey(t)
	fake := &fakeGitHub{t: t, key: &key.PublicKey, now: t0, repos: repos}
	srv := httptest.NewServer(fake)
	t.Cleanup(srv.Close)
	pemKey := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	app, err := newAppClient("123456", 77, pemKey)
	if err != nil {
		t.Fatal(err)
	}
	app.api, app.now = srv.URL, func() time.Time { return t0 }
	return app, fake
}

func repo(name string, private, archived bool) installationRepo {
	r := installationRepo{Name: name, Private: private, Archived: archived}
	r.Owner.Login = "example"
	return r
}

func TestRepositoriesPagesThroughTheInstallation(t *testing.T) {
	var repos []installationRepo
	for i := range 230 {
		repos = append(repos, repo(fmt.Sprintf("repo-%03d", i), true, false))
	}
	app, fake := testApp(t, repos)
	got, err := app.repositories(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 230 || got[229].Name != "repo-229" {
		t.Errorf("got %d repositories", len(got))
	}
	if !reflect.DeepEqual(fake.pages, []int{1, 2, 3}) || fake.tokens != 1 {
		t.Errorf("pages %v, tokens %d", fake.pages, fake.tokens)
	}
}

func TestRepositoriesReportsAFailedTokenWithoutSecrets(t *testing.T) {
	app, fake := testApp(t, nil)
	fake.tokenErr = http.StatusUnauthorized
	_, err := app.repositories(context.Background())
	if err == nil || !strings.Contains(err.Error(), "HTTP 401") {
		t.Fatalf("err = %v", err)
	}
	if strings.Contains(err.Error(), "eyJ") || strings.Contains(err.Error(), "PRIVATE KEY") {
		t.Errorf("the error carries credential material: %v", err)
	}
}

func TestParsePrivateKeyAcceptsPKCS1AndPKCS8(t *testing.T) {
	key := appKey(t)
	pkcs8, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	for name, block := range map[string]*pem.Block{
		"pkcs1": {Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)},
		"pkcs8": {Type: "PRIVATE KEY", Bytes: pkcs8},
	} {
		got, err := parsePrivateKey(pem.EncodeToMemory(block))
		if err != nil || !got.Equal(key) {
			t.Errorf("%s: %v", name, err)
		}
	}
	if _, err := parsePrivateKey([]byte("not a key")); err == nil {
		t.Error("garbage accepted as a key")
	}
}

func TestServedTargets(t *testing.T) {
	other := repo("elsewhere", true, false)
	other.Owner.Login = "someone-else"
	repos := []installationRepo{
		repo("app", true, false),
		repo("site", false, false), // public: never served
		repo("old", true, true),    // archived
		repo("Fleet", true, false), // excluded, case-insensitively
		repo("tool", true, false),
		other, // not the lane's owner
	}
	served, unserved := servedTargets(repos, "example", []string{"fleet"})
	if !reflect.DeepEqual(served, []string{"example/app", "example/tool"}) {
		t.Errorf("served = %v", served)
	}
	if !reflect.DeepEqual(unserved, []string{"example/site", "example/old", "example/Fleet"}) {
		t.Errorf("unserved = %v", unserved)
	}
}
