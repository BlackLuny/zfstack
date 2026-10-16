000000000006d240 <zfstack::buf::BlockPool::put>:
   6d240:	push   %rbp
   6d241:	push   %r15
   6d243:	push   %r14
   6d245:	push   %r13
   6d247:	push   %r12
   6d249:	push   %rbx
   6d24a:	sub    $0x58,%rsp
   6d24e:	mov    %rsi,%rbx
   6d251:	mov    0xc8(%rdi),%r15
   6d258:	test   %r15,%r15
   6d25b:	je     6d27c <zfstack::buf::BlockPool::put+0x3c>
   6d25d:	mov    0xa8(%rdi),%rax
   6d264:	test   %rax,%rax
   6d267:	je     6d27c <zfstack::buf::BlockPool::put+0x3c>
   6d269:	cmp    0x30(%rbx),%rax
   6d26d:	jne    6d27c <zfstack::buf::BlockPool::put+0x3c>
   6d26f:	mov    0xb0(%rdi),%rax
   6d276:	cmp    0x38(%rbx),%rax
   6d27a:	je     6d2a6 <zfstack::buf::BlockPool::put+0x66>
   6d27c:	cmpq   $0x0,0x8(%rbx)
   6d281:	je     6d28c <zfstack::buf::BlockPool::put+0x4c>
   6d283:	mov    (%rbx),%rdi
   6d286:	call   *0x44b74(%rip)        # b1e00 <free@GLIBC_2.2.5>
   6d28c:	add    $0x18,%rbx
   6d290:	mov    %rbx,%rdi
   6d293:	add    $0x58,%rsp
   6d297:	pop    %rbx
   6d298:	pop    %r12
   6d29a:	pop    %r13
   6d29c:	pop    %r14
   6d29e:	pop    %r15
   6d2a0:	pop    %rbp
   6d2a1:	jmp    6d120 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
   6d2a6:	mov    0x10(%rdi),%r14
   6d2aa:	mov    0x28(%rdi),%r12
   6d2ae:	lea    (%r12,%r14,1),%rax
   6d2b2:	cmp    %r15,%rax
   6d2b5:	jne    6d37e <zfstack::buf::BlockPool::put+0x13e>
   6d2bb:	cmpq   $0x800,0x8(%rbx)
   6d2c3:	jne    6d2de <zfstack::buf::BlockPool::put+0x9e>
   6d2c5:	test   %r14,%r14
   6d2c8:	je     6d327 <zfstack::buf::BlockPool::put+0xe7>
   6d2ca:	dec    %r14
   6d2cd:	mov    %r14,0x10(%rdi)
   6d2d1:	mov    %rdi,%r13
   6d2d4:	mov    0x8(%rdi),%rax
   6d2d8:	lea    (%r14,%r14,8),%rcx
   6d2dc:	jmp    6d2f5 <zfstack::buf::BlockPool::put+0xb5>
   6d2de:	test   %r12,%r12
   6d2e1:	je     6d334 <zfstack::buf::BlockPool::put+0xf4>
   6d2e3:	dec    %r12
   6d2e6:	mov    %r12,0x28(%rdi)
   6d2ea:	mov    %rdi,%r13
   6d2ed:	mov    0x20(%rdi),%rax
   6d2f1:	lea    (%r12,%r12,8),%rcx
   6d2f5:	movups (%rax,%rcx,8),%xmm0
   6d2f9:	movups 0x10(%rax,%rcx,8),%xmm1
   6d2fe:	movups 0x20(%rax,%rcx,8),%xmm2
   6d303:	movups 0x30(%rax,%rcx,8),%xmm3
   6d308:	mov    0x40(%rax,%rcx,8),%rax
   6d30d:	mov    %rax,0x40(%rsp)
   6d312:	movaps %xmm3,0x30(%rsp)
   6d317:	movaps %xmm2,0x20(%rsp)
   6d31c:	movaps %xmm1,0x10(%rsp)
   6d321:	movaps %xmm0,(%rsp)
   6d325:	jmp    6d33f <zfstack::buf::BlockPool::put+0xff>
   6d327:	mov    %rdi,%r13
   6d32a:	movb   $0x2,0x42(%rsp)
   6d32f:	xor    %r14d,%r14d
   6d332:	jmp    6d33f <zfstack::buf::BlockPool::put+0xff>
   6d334:	mov    %rdi,%r13
   6d337:	movb   $0x2,0x42(%rsp)
   6d33c:	xor    %r12d,%r12d
   6d33f:	mov    %rsp,%rdi
   6d342:	call   6d5f0 <core::ptr::drop_in_place<core::option::Option<zfstack::buf::Block>>>
   6d347:	mov    %r13,%rdi
   6d34a:	cmpb   $0x2,0x40(%r13)
   6d34f:	je     6d37e <zfstack::buf::BlockPool::put+0x13e>
   6d351:	mov    0x30(%rdi),%rax
   6d355:	cmp    %r14,%rax
   6d358:	cmovb  %rax,%r14
   6d35c:	mov    %r14,0x30(%rdi)
   6d360:	mov    0x38(%rdi),%rax
   6d364:	cmp    %r12,%rax
   6d367:	cmovb  %rax,%r12
   6d36b:	mov    %r12,0x38(%rdi)
   6d36f:	mov    0x10(%rdi),%r14
   6d373:	mov    0x28(%rdi),%r12
   6d377:	mov    0xc8(%rdi),%r15
   6d37e:	add    %r14,%r12
   6d381:	lea    0x18(%rbx),%r14
   6d385:	cmp    %r15,%r12
   6d388:	jae    6d3c3 <zfstack::buf::BlockPool::put+0x183>
   6d38a:	mov    0x30(%rbx),%rax
   6d38e:	mov    0x40(%rax),%rax
   6d392:	test   %rax,%rax
   6d395:	jne    6d3c3 <zfstack::buf::BlockPool::put+0x183>
   6d397:	lea    0x38(%rbx),%rcx
   6d39b:	mov    (%rcx),%rax
   6d39e:	mov    0x30(%rax),%rax
   6d3a2:	test   %rax,%rax
   6d3a5:	jne    6d3c3 <zfstack::buf::BlockPool::put+0x183>
   6d3a7:	mov    0x30(%rbx),%rax
   6d3ab:	mov    0xa8(%rax),%rdx
   6d3b2:	test   %rdx,%rdx
   6d3b5:	jne    6d3c3 <zfstack::buf::BlockPool::put+0x183>
   6d3b7:	mov    0x110(%rax),%rax
   6d3be:	test   %rax,%rax
   6d3c1:	je     6d3ea <zfstack::buf::BlockPool::put+0x1aa>
   6d3c3:	cmpq   $0x0,0x8(%rbx)
   6d3c8:	je     6d3d3 <zfstack::buf::BlockPool::put+0x193>
   6d3ca:	mov    (%rbx),%rdi
   6d3cd:	call   *0x44a2d(%rip)        # b1e00 <free@GLIBC_2.2.5>
   6d3d3:	mov    %r14,%rdi
   6d3d6:	call   6d120 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
   6d3db:	add    $0x58,%rsp
   6d3df:	pop    %rbx
   6d3e0:	pop    %r12
   6d3e2:	pop    %r13