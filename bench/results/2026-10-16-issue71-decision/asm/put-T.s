000000000006d240 <zfstack::buf::BlockPool::put>:
   6d240:	push   %rbp
   6d241:	push   %r15
   6d243:	push   %r14
   6d245:	push   %r13
   6d247:	push   %r12
   6d249:	push   %rbx
   6d24a:	sub    $0x128,%rsp
   6d251:	mov    %rsi,%rbx
   6d254:	mov    0xa8(%rdi),%rax
   6d25b:	test   %rax,%rax
   6d25e:	je     6d273 <zfstack::buf::BlockPool::put+0x33>
   6d260:	cmp    0x30(%rbx),%rax
   6d264:	jne    6d273 <zfstack::buf::BlockPool::put+0x33>
   6d266:	mov    0xb0(%rdi),%rax
   6d26d:	cmp    0x38(%rbx),%rax
   6d271:	je     6d2a0 <zfstack::buf::BlockPool::put+0x60>
   6d273:	cmpq   $0x0,0x8(%rbx)
   6d278:	je     6d283 <zfstack::buf::BlockPool::put+0x43>
   6d27a:	mov    (%rbx),%rdi
   6d27d:	call   *0x43b7d(%rip)        # b0e00 <free@GLIBC_2.2.5>
   6d283:	add    $0x18,%rbx
   6d287:	mov    %rbx,%rdi
   6d28a:	add    $0x128,%rsp
   6d291:	pop    %rbx
   6d292:	pop    %r12
   6d294:	pop    %r13
   6d296:	pop    %r14
   6d298:	pop    %r15
   6d29a:	pop    %rbp
   6d29b:	jmp    6d120 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
   6d2a0:	mov    0x10(%rdi),%rcx
   6d2a4:	mov    0x28(%rdi),%rax
   6d2a8:	lea    (%rax,%rcx,1),%rdx
   6d2ac:	cmp    0xc8(%rdi),%rdx
   6d2b3:	jne    6d398 <zfstack::buf::BlockPool::put+0x158>
   6d2b9:	cmpq   $0x800,0x8(%rbx)
   6d2c1:	jne    6d2f5 <zfstack::buf::BlockPool::put+0xb5>
   6d2c3:	test   %rcx,%rcx
   6d2c6:	je     6d364 <zfstack::buf::BlockPool::put+0x124>
   6d2cc:	dec    %rcx
   6d2cf:	mov    %rcx,0x10(%rdi)
   6d2d3:	mov    0x8(%rdi),%rax
   6d2d7:	lea    (%rcx,%rcx,8),%rcx
   6d2db:	movups (%rax,%rcx,8),%xmm0
   6d2df:	movups 0x10(%rax,%rcx,8),%xmm1
   6d2e4:	movups 0x20(%rax,%rcx,8),%xmm2
   6d2e9:	movups 0x30(%rax,%rcx,8),%xmm3
   6d2ee:	mov    0x40(%rax,%rcx,8),%rax
   6d2f3:	jmp    6d321 <zfstack::buf::BlockPool::put+0xe1>
   6d2f5:	test   %rax,%rax
   6d2f8:	je     6d364 <zfstack::buf::BlockPool::put+0x124>
   6d2fa:	dec    %rax
   6d2fd:	mov    %rax,0x28(%rdi)
   6d301:	mov    0x20(%rdi),%rcx
   6d305:	lea    (%rax,%rax,8),%rax
   6d309:	movups (%rcx,%rax,8),%xmm0
   6d30d:	movups 0x10(%rcx,%rax,8),%xmm1
   6d312:	movups 0x20(%rcx,%rax,8),%xmm2
   6d317:	movups 0x30(%rcx,%rax,8),%xmm3
   6d31c:	mov    0x40(%rcx,%rax,8),%rax
   6d321:	mov    %rax,0x60(%rsp)
   6d326:	movaps %xmm3,0x50(%rsp)
   6d32b:	movaps %xmm2,0x40(%rsp)
   6d330:	movaps %xmm1,0x30(%rsp)
   6d335:	movaps %xmm0,0x20(%rsp)
   6d33a:	cmpb   $0x2,0x62(%rsp)
   6d33f:	je     6d364 <zfstack::buf::BlockPool::put+0x124>
   6d341:	mov    %rdi,%r14
   6d344:	cmpq   $0x0,0x28(%rsp)
   6d34a:	je     6d357 <zfstack::buf::BlockPool::put+0x117>
   6d34c:	mov    0x20(%rsp),%rdi
   6d351:	call   *0x43aa9(%rip)        # b0e00 <free@GLIBC_2.2.5>
   6d357:	lea    0x38(%rsp),%rdi
   6d35c:	call   6d120 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
   6d361:	mov    %r14,%rdi
   6d364:	cmpb   $0x2,0x40(%rdi)
   6d368:	mov    0x10(%rdi),%rcx
   6d36c:	mov    0x28(%rdi),%rax
   6d370:	je     6d398 <zfstack::buf::BlockPool::put+0x158>
   6d372:	mov    0x30(%rdi),%rdx
   6d376:	cmp    %rcx,%rdx
   6d379:	cmovb  %rdx,%rcx
   6d37d:	mov    %rcx,0x30(%rdi)
   6d381:	mov    0x38(%rdi),%rcx
   6d385:	cmp    %rax,%rcx
   6d388:	cmovb  %rcx,%rax
   6d38c:	mov    %rax,0x38(%rdi)
   6d390:	mov    0x10(%rdi),%rcx
   6d394:	mov    0x28(%rdi),%rax
   6d398:	add    %rcx,%rax
   6d39b:	lea    0x18(%rbx),%r14
   6d39f:	cmp    0xc8(%rdi),%rax
   6d3a6:	jae    6d3e1 <zfstack::buf::BlockPool::put+0x1a1>
   6d3a8:	mov    0x30(%rbx),%rax
   6d3ac:	mov    0x40(%rax),%rax
   6d3b0:	test   %rax,%rax
   6d3b3:	jne    6d3e1 <zfstack::buf::BlockPool::put+0x1a1>
   6d3b5:	lea    0x38(%rbx),%rcx
   6d3b9:	mov    (%rcx),%rax
   6d3bc:	mov    0x30(%rax),%rax
   6d3c0:	test   %rax,%rax
   6d3c3:	jne    6d3e1 <zfstack::buf::BlockPool::put+0x1a1>
   6d3c5:	mov    0x30(%rbx),%rax
   6d3c9:	mov    0xa8(%rax),%rdx
   6d3d0:	test   %rdx,%rdx
   6d3d3:	jne    6d3e1 <zfstack::buf::BlockPool::put+0x1a1>
   6d3d5:	mov    0x110(%rax),%rax
   6d3dc:	test   %rax,%rax
   6d3df:	je     6d40b <zfstack::buf::BlockPool::put+0x1cb>
   6d3e1:	cmpq   $0x0,0x8(%rbx)
   6d3e6:	je     6d3f1 <zfstack::buf::BlockPool::put+0x1b1>
   6d3e8:	mov    (%rbx),%rdi
   6d3eb:	call   *0x43a0f(%rip)        # b0e00 <free@GLIBC_2.2.5>
   6d3f1:	mov    %r14,%rdi
   6d3f4:	call   6d120 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
   6d3f9:	add    $0x128,%rsp
   6d400:	pop    %rbx
   6d401:	pop    %r12
   6d403:	pop    %r13