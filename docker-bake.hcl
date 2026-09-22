group "default" {
  targets = ["mcp-portal", "billing-mock", "orders-mock"]
}

target "base-builder" {
  dockerfile = "Dockerfile.base"
  target     = "base-builder"
}

target "base-runtime" {
  dockerfile = "Dockerfile.base"
  target     = "base-runtime"
}

target "mcp-portal" {
  context    = "."
  dockerfile = "Dockerfile"
  tags       = ["mcp-portal:local"]
  contexts = {
    pybuilder = "target:base-builder"
    pyruntime = "target:base-runtime"
  }
}

target "billing-mock" {
  context    = "./mocks/billing"
  dockerfile = "Dockerfile"
  tags       = ["billing-mock:local"]
  contexts = {
    pybuilder = "target:base-builder"
    pyruntime = "target:base-runtime"
  }
}

target "orders-mock" {
  context    = "./mocks/orders"
  dockerfile = "Dockerfile"
  tags       = ["orders-mock:local"]
  contexts = {
    pybuilder = "target:base-builder"
    pyruntime = "target:base-runtime"
  }
}
