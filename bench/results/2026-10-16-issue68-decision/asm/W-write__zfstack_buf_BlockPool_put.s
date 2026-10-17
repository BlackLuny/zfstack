   73ee0:	push   %rbp
   73ee1:	push   %r15
   73ee3:	push   %r14
   73ee5:	push   %r13
   73ee7:	push   %r12
   73ee9:	push   %rbx
   73eea:	sub    $0x98,%rsp
   73ef1:	mov    %rsi,%rbx
   73ef4:	cmpb   $0x3,0xf3(%rdi)
   73efb:	je     73f17 <zfstack::buf::BlockPool::put+0x37>
   73efd:	mov    0xb0(%rdi),%rax
   73f04:	cmp    0x30(%rbx),%rax
   73f08:	jne    73f17 <zfstack::buf::BlockPool::put+0x37>
   73f0a:	mov    0xb8(%rdi),%rax
   73f11:	cmp    0x38(%rbx),%rax
   73f15:	je     73f44 <zfstack::buf::BlockPool::put+0x64>
   73f17:	cmpq   $0x0,0x8(%rbx)
   73f1c:	je     73f27 <zfstack::buf::BlockPool::put+0x47>
   73f1e:	mov    (%rbx),%rdi
   73f21:	call   *0x45ed9(%rip)        # b9e00 <free@GLIBC_2.2.5>
   73f27:	add    $0x18,%rbx
   73f2b:	mov    %rbx,%rdi
   73f2e:	add    $0x98,%rsp
   73f35:	pop    %rbx
   73f36:	pop    %r12
   73f38:	pop    %r13
   73f3a:	pop    %r14
   73f3c:	pop    %r15
   73f3e:	pop    %rbp
   73f3f:	jmp    73510 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
   73f44:	mov    0xf8(%rdi),%rdx
   73f4b:	test   %rdx,%rdx
   73f4e:	je     74085 <zfstack::buf::BlockPool::put+0x1a5>
   73f54:	mov    0x10(%rdi),%rcx
   73f58:	mov    0x28(%rdi),%rax
   73f5c:	lea    (%rax,%rcx,1),%rsi
   73f60:	cmp    %rdx,%rsi
   73f63:	jne    74047 <zfstack::buf::BlockPool::put+0x167>
   73f69:	cmpq   $0x800,0x8(%rbx)
   73f71:	jne    73f89 <zfstack::buf::BlockPool::put+0xa9>
