// Instrument a replay copy, never the delivered Go fixture.
package main

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/format"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type rewriter struct {
	owner    *ast.Ident
	imports  map[string]string
	seen     map[*ast.CallExpr]bool
	files    *token.FileSet
	count    *int
	problems *[]error
}

func (v rewriter) packageMember(expr ast.Expr, pkg, member string) bool {
	if selector, ok := expr.(*ast.SelectorExpr); ok {
		id, ok := selector.X.(*ast.Ident)
		return ok && id.Obj == nil && v.imports[id.Name] == pkg && selector.Sel.Name == member
	}
	id, ok := expr.(*ast.Ident)
	return ok && id.Obj == nil && v.imports["."] == pkg && id.Name == member
}

func (v rewriter) testParameter(params *ast.FieldList) *ast.Ident {
	if params == nil {
		return nil
	}
	for _, field := range params.List {
		typ := field.Type
		if pointer, ok := typ.(*ast.StarExpr); ok {
			typ = pointer.X
		}
		for _, name := range []string{"T", "B", "F", "TB"} {
			if v.packageMember(typ, "testing", name) && len(field.Names) == 1 {
				return field.Names[0]
			}
		}
	}
	return nil
}

func (v rewriter) Visit(node ast.Node) ast.Visitor {
	switch node := node.(type) {
	case *ast.FuncDecl:
		v.owner = v.testParameter(node.Type.Params)
	case *ast.FuncLit:
		if owner := v.testParameter(node.Type.Params); owner != nil {
			v.owner = owner
		}
	case *ast.CallExpr:
		if v.seen[node] {
			break
		}
		for _, factory := range []string{"NewServer", "NewTLSServer", "NewUnstartedServer"} {
			if !v.packageMember(node.Fun, "net/http/httptest", factory) {
				continue
			}
			if v.owner == nil {
				*v.problems = append(*v.problems, fmt.Errorf(
					"%s: server factory has no testing.T/TB owner; this fixture probe requires an owner parameter",
					v.files.Position(node.Pos())))
				return nil
			}
			inner := *node
			if selector, ok := inner.Fun.(*ast.SelectorExpr); ok {
				inner.Fun = &ast.SelectorExpr{X: selector.X, Sel: ast.NewIdent("NewUnstartedServer")}
			} else {
				inner.Fun = ast.NewIdent("NewUnstartedServer")
			}
			v.seen[&inner] = true
			*node = ast.CallExpr{
				Fun: ast.NewIdent("evalLifetimeServer"),
				Args: []ast.Expr{
					ast.NewIdent(v.owner.Name), &inner,
					&ast.BasicLit{Kind: token.STRING, Value: strconv.Quote(factory)},
					&ast.BasicLit{Kind: token.STRING, Value: strconv.Quote(v.files.Position(node.Pos()).String())},
				},
			}
			*v.count++
			break
		}
	}
	return v
}

const probe = `
package %s

import (
    "net"
    "net/http/httptest"
    "sync/atomic"
    "testing"
)

type evalLifetimeListener struct {
    net.Listener
    closed atomic.Bool
}

func (l *evalLifetimeListener) Close() error {
    err := l.Listener.Close()
    l.closed.Store(true)
    return err
}

func evalLifetimeServer(t testing.TB, server *httptest.Server, factory, location string) *httptest.Server {
    t.Helper()
    listener := &evalLifetimeListener{Listener: server.Listener}
    server.Listener = listener
    t.Cleanup(func() {
        if !listener.closed.Load() {
            t.Errorf("GO_SERVER_LIFETIME: unclosed server at %%s", location)
            server.Close()
        } else {
            t.Logf("GO_SERVER_LIFETIME: verified %%s", location)
        }
    })
    switch factory {
    case "NewServer":
        server.Start()
    case "NewTLSServer":
        server.StartTLS()
    }
    return server
}
`

func run(root string) error {
	files, err := filepath.Glob(filepath.Join(root, "*_test.go"))
	if err != nil {
		return err
	}
	count := 0
	packages := map[string]bool{}
	for _, filename := range files {
		fset := token.NewFileSet()
		tree, err := parser.ParseFile(fset, filename, nil, parser.ParseComments)
		if err != nil {
			return err
		}
		imports := map[string]string{}
		for _, spec := range tree.Imports {
			pkg, err := strconv.Unquote(spec.Path.Value)
			if err != nil {
				return err
			}
			name := filepath.Base(pkg)
			if spec.Name != nil {
				name = spec.Name.Name
			}
			imports[name] = pkg
		}
		before := count
		var problems []error
		ast.Walk(rewriter{
			imports: imports, seen: map[*ast.CallExpr]bool{},
			files: fset, count: &count, problems: &problems,
		}, tree)
		if len(problems) != 0 {
			return problems[0]
		}
		if before == count {
			continue
		}
		packages[tree.Name.Name] = true
		var rewritten bytes.Buffer
		if err := format.Node(&rewritten, fset, tree); err != nil {
			return err
		}
		if err := os.WriteFile(filename, rewritten.Bytes(), 0600); err != nil {
			return err
		}
	}
	if count == 0 {
		return fmt.Errorf("no owned httptest server factories found in the Go tests")
	}
	for pkg := range packages {
		filename := filepath.Join(root, "zz_eval_lifetime_"+pkg+"_test.go")
		data, err := format.Source([]byte(fmt.Sprintf(probe, pkg)))
		if err != nil {
			return err
		}
		out, err := os.OpenFile(filename, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			return err
		}
		_, writeErr := out.Write(data)
		closeErr := out.Close()
		if writeErr != nil {
			return writeErr
		}
		if closeErr != nil {
			return closeErr
		}
	}
	fmt.Printf("GO_SERVER_LIFETIME: instrumented %d factories\n", count)
	return nil
}

func main() {
	if len(os.Args) != 2 || strings.TrimSpace(os.Args[1]) == "" {
		fmt.Fprintln(os.Stderr, "usage: go_server_lifetime <replay-directory>")
		os.Exit(2)
	}
	if err := run(os.Args[1]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
